# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""PGP encryption and decryption of files, in a child process.

pgpy is pure python where it matters for big files: it checks and makes the
armor's CRC24 one bit at a time, which for a photo of 15 MB is over 100 million
steps of the interpreter. Done in a thread of Blink, that holds the GIL for
many seconds and starves every other thread: the SIP reactor (DNS, XCAP and SIP
timers), the GUI. So files go through a plain python3 process that imports pgpy
only (as blink.pgp_decryptor does for messages), and the armor is done there with
binascii: base64 in C, the CRC24 from a table, not checked when reading (the
integrity of the message is in the encrypted packet, MDC or AEAD, which pgpy checks).

One request per process, JSON on stdin, one JSON line on stdout:
  {"op": "decrypt", "file": armored or binary file, "out": where to write the
   plaintext, "keys": [armored private key, ...]}
      -> {"key": index of the key that opened it} or {"error": ...} or {"not_pgp": ...}
  {"op": "encrypt", "file": the plain file, "out": where to write the armored
   message, "keys": [armored public key, ...]}
      -> {"ok": true} or {"error": ...}
"""

__all__ = ['decrypt_file', 'encrypt_file', 'NotPGPError']

import json
import os
import subprocess
import sys
import tempfile


WORKER_CODE = r"""
import binascii, json, os, sys, warnings
warnings.simplefilter('ignore')
try:
    os.nice(5)
except OSError:
    pass

def crc24_table():
    table = []
    for i in range(256):
        c = i << 16
        for _ in range(8):
            c <<= 1
            if c & 0x1000000:
                c ^= 0x1864CFB
        table.append(c & 0xFFFFFF)
    return table

def crc24(data):
    table, crc = crc24_table(), 0xB704CE
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ table[(crc >> 16) ^ b]
    return crc

def armor(binary):
    b64 = binascii.b2a_base64(binary, newline=False)
    body = b'\n'.join(b64[i:i + 64] for i in range(0, len(b64), 64))
    crc = binascii.b2a_base64(crc24(binary).to_bytes(3, 'big'), newline=False)
    return b'-----BEGIN PGP MESSAGE-----\n\n' + body + b'\n=' + crc + b'\n-----END PGP MESSAGE-----\n'

def unarmor(data):
    # binary as it is; armored: the base64 body between the headers and the CRC (or the END line)
    if not data.lstrip()[:15] == b'-----BEGIN PGP ':
        return data
    lines = data.strip().splitlines()
    i = 1
    while i < len(lines) and b': ' in lines[i]:       # headers (Version, Comment)
        i += 1
    body = []
    for line in lines[i:]:
        line = line.strip()
        if line.startswith(b'=') or line.startswith(b'-----'):
            break
        body.append(line)
    return binascii.a2b_base64(b''.join(body))

def decrypt(request):
    import pgpy
    with open(request['file'], 'rb') as f:
        data = f.read()
    try:
        message = pgpy.PGPMessage.from_blob(unarmor(data))
    except Exception as e:
        return {'not_pgp': str(e) or e.__class__.__name__}
    error = 'no keys'
    for index, armored in enumerate(request['keys']):
        try:
            key = pgpy.PGPKey.from_blob(armored)[0]
            decrypted = key.decrypt(message)
        except Exception as e:
            error = str(e) or e.__class__.__name__
            continue
        # the literal data's bytes as sent: pgpy's .message decodes data marked as text
        # ('t' latin-1, 'u' UTF-8), which fails for a binary file marked 'u' (some clients do)
        # and would change a UTF-8 text file marked 't'
        contents = getattr(getattr(decrypted, '_message', None), '_contents', None)
        if contents is None:
            contents = decrypted.message
        contents = bytes(contents) if isinstance(contents, (bytes, bytearray)) else contents.encode('utf-8')
        with open(request['out'], 'wb') as f:
            f.write(contents)
        return {'key': index}
    return {'error': error}

def encrypt(request):
    import pgpy
    from pgpy.constants import SymmetricKeyAlgorithm, CompressionAlgorithm
    message = pgpy.PGPMessage.new(request['file'], file=True, compression=CompressionAlgorithm.Uncompressed)
    cipher = SymmetricKeyAlgorithm.AES256
    sessionkey = cipher.gen_key()
    for armored in request['keys']:
        message = pgpy.PGPKey.from_blob(armored)[0].encrypt(message, cipher=cipher, sessionkey=sessionkey)
    del sessionkey
    with open(request['out'], 'wb') as f:
        f.write(armor(bytes(message)))
    return {'ok': True}

try:
    request = json.loads(sys.stdin.read())
    answer = {'decrypt': decrypt, 'encrypt': encrypt}[request['op']](request)
except Exception as e:
    answer = {'error': str(e) or e.__class__.__name__}
sys.stdout.write(json.dumps(answer) + '\n')
"""


class NotPGPError(Exception):
    pass


def _run(request, timeout=600):
    process = subprocess.run([sys.executable, '-I', '-c', WORKER_CODE], input=json.dumps(request), capture_output=True, text=True, timeout=timeout)
    lines = process.stdout.strip().splitlines()
    if not lines:
        error = process.stderr.strip().splitlines()
        raise RuntimeError(error[-1] if error else f'the PGP worker ended with code {process.returncode}')
    return json.loads(lines[-1])


def decrypt_file(filename, output, keys):
    """Writes the plaintext of filename to output. keys: armored private keys; returns the index
    of the one that opened it. Raises NotPGPError, or RuntimeError with the reason. Blocks."""
    answer = _run({'op': 'decrypt', 'file': filename, 'out': output, 'keys': list(keys)})
    if 'not_pgp' in answer:
        raise NotPGPError(answer['not_pgp'])
    if 'error' in answer:
        raise RuntimeError(answer['error'])
    return answer['key']


def encrypt_file(filename, keys):
    """The armored PGP message (bytes) of filename for the armored public keys. Raises
    RuntimeError. Blocks."""
    fd, output = tempfile.mkstemp(prefix='blink-', suffix='.asc')
    os.close(fd)
    try:
        answer = _run({'op': 'encrypt', 'file': filename, 'out': output, 'keys': list(keys)})
        if 'error' in answer:
            raise RuntimeError(answer['error'])
        with open(output, 'rb') as f:
            return f.read()
    finally:
        try:
            os.unlink(output)
        except OSError:
            pass
