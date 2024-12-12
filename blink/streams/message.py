import os

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import makedirs, unlink, openfile, FileExistsError

from otr import OTRTransport
from otr.exceptions import IgnoreMessage, UnencryptedMessage, EncryptedMessageError, OTRError, OTRFinishedError
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import IMediaStream, MediaStreamType, UnknownStreamError
from sipsimple.streams.msrp.chat import OTREncryption
from sipsimple.threading import run_in_thread
from sipsimple.threading.green import run_in_green_thread

from zope.interface import implementer

from pgpy import PGPKey, PGPUID, PGPMessage
from pgpy.errors import PGPError, PGPDecryptionError
from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm, CompressionAlgorithm

from blink.logging import MessagingTrace as log
from blink.util import run_in_gui_thread, UniqueFilenameGenerator


__all__ = ['MessageStream']


@implementer(IMediaStream, IObserver)
class MessageStream(object, metaclass=MediaStreamType):
    type = 'messages'
    priority = 20

    hold_supported = False
    on_hold = False
    on_hold_by_local = False
    on_hold_by_remote = False

    def __init__(self, **kw):
        for keyword in kw:
            self.keyword = kw[keyword]
        self.private_key = None
        self.public_key = None
        self.remote_public_key = None
        self.other_private_keys = []
        self.encryption = OTREncryption(self)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='PGPKeysShouldReload')

    @run_in_green_thread
    def initialize(self, session, direction):
        pass

    @run_in_green_thread
    def start(self, local_sdp, remote_sdp, stream_index):
        pass

    def validate_update(self, remote_sdp, stream_index):
        return True

    def update(self, local_sdp, remote_sdp, stream_index):
        pass

    def hold(self):
        pass

    def unhold(self):
        pass

    def reset(self, stream_index):
        pass

    def connect(self):
        pass

    def deactivate(self):
        pass

    def end(self):
        pass

    @property
    def local_uri(self):
        return None

    @property
    def msrp(self):
        return None

    @property
    def can_encrypt(self):
        return self.private_key is not None and self.remote_public_key is not None

    @property
    def can_decrypt(self):
        return self.private_key is not None

    @property
    def can_decrypt_with_others(self):
        return len(self.other_private_keys) > 0

    def _get_private_key(self):
        return self.__dict__['private_key']

    def _set_private_key(self, value):
        self.__dict__['private_key'] = value

    private_key = property(_get_private_key, _set_private_key)

    del _get_private_key, _set_private_key

    def _get_public_key(self):
        return self.__dict__['public_key']

    def _set_public_key(self, value):
        self.__dict__['public_key'] = value

    public_key = property(_get_public_key, _set_public_key)

    del _get_public_key, _set_public_key

    def _get_remote_public_key(self):
        return self.__dict__['remote_public_key']

    def _set_remote_public_key(self, value):
        self.__dict__['remote_public_key'] = value

    remote_public_key = property(_get_remote_public_key, _set_remote_public_key)

    del _get_remote_public_key, _set_remote_public_key

    @classmethod
    def new_from_sdp(cls, session, remote_sdp, stream_index):
        raise UnknownStreamError

    @run_in_thread('file-io')
    def generate_keys(self):
        session = self.blink_session
        log.info(f'-- Generating key for {session.account.uri}')
        private_key = PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 4096)
        uid = PGPUID.new(session.account.display_name, comment='Blink QT client', email=session.account.id)
        private_key.add_uid(uid,
                            usage={KeyFlags.Sign, KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
                            hashes=[HashAlgorithm.SHA512],
                            ciphers=[SymmetricKeyAlgorithm.AES256],
                            compression=[CompressionAlgorithm.Uncompressed])

        settings = SIPSimpleSettings()
        directory = os.path.join(settings.chat.keys_directory.normalized, 'private')
        filename = os.path.join(directory, session.account.id)
        makedirs(directory)

        with open(f'{filename}.privkey', 'wb') as f:
            f.write(str(private_key).encode())

        with open(f'{filename}.pubkey', 'wb') as f:
            f.write(str(private_key.pubkey).encode())

        session.account.sms.private_key = f'{filename}.privkey'
        session.account.sms.public_key = f'{filename}.pubkey'
        session.account.save()
        self._load_pgp_keys()

        notification_center = NotificationCenter()
        notification_center.post_notification('PGPKeysDidGenerate', sender=session, data=NotificationData(private_key=private_key, public_key=private_key.pubkey))

    def inject_otr_message(self, data):
        from blink.messages import MessageManager
        MessageManager().send_otr_message(self.blink_session, data)

    def enable_pgp(self):
        self._load_pgp_keys()

    def enable_otr(self):
        self.encryption.start()

    def disable_otr(self):
        self.encryption.stop()

    def check_otr(self, message):
        content = None
        notification_center = NotificationCenter()
        try:
            content = self.encryption.otr_session.handle_input(message.content.encode(), message.content_type)
        except IgnoreMessage:
            return None
        except UnencryptedMessage:
            return message
        except EncryptedMessageError as e:
            log.warning(f'OTR encrypted message error: {e}')
            return None
        except OTRFinishedError:
            log.info('OTR has finished')
            return None
        except OTRError as e:
            log.warning(f'OTR message error: {e}')
            return None
        else:
            content = content.decode() if isinstance(content, bytes) else content

            if content.startswith('?OTR:'):
                notification_center.post_notification('ChatStreamOTRError', sender=self, data=NotificationData(error='OTR message could not be decoded'))
                log.warning('OTR message could not be decoded')
                if self.encryption.active:
                    self.encryption.stop()
                return None

            log.info("Message uses OTR encryption, message is decoded")
            message.is_secure = self.encryption.active
            message.content = content.decode() if isinstance(content, bytes) else content
            return message

    def encrypt(self, content, content_type=None):
        # print('-- Encrypting message')
        if self.encryption.active:
            try:
                encrypted_content = self.encryption.otr_session.handle_output(content.encode(), content_type)
            except OTRError as e:
                log.info("Encryption failed OTR encryption has been disabled by remote party")
                self.encryption.stop()
                raise Exception(f"OTR encryption has been disabled by remote party {e}")
            except OTRFinishedError:
                log.info("OTR encryption has been disabled by remote party")
                self.encryption.stop()
                raise Exception("OTR encryption has been disabled by remote party")
            else:
                if not encrypted_content.startswith(b'?OTR:'):
                    self.encryption.stop()
                    log.info("OTR encryption has been stopped")
                    raise Exception("OTR encryption has been stopped")
                return str(encrypted_content.decode())

        pgp_message = PGPMessage.new(content, compression=CompressionAlgorithm.Uncompressed)
        cipher = SymmetricKeyAlgorithm.AES256

        sessionkey = cipher.gen_key()
        encrypted_content = self.public_key.encrypt(pgp_message, cipher=cipher, sessionkey=sessionkey)
        encrypted_content = self.remote_public_key.encrypt(encrypted_content, cipher=cipher, sessionkey=sessionkey)
        del sessionkey
        return str(encrypted_content)

    @run_in_thread('pgp')
    def decrypt(self, message):
        session = self.blink_session
        notification_center = NotificationCenter()

        if self.private_key is None and len(self.other_private_keys) == 0:
            notification_center.post_notification('PGPMessageDidNotDecrypt', sender=session, data=NotificationData(message=message), error='No private key')
            return

        try:
            msg_id = message.message_id
        except AttributeError:
            msg_id = message.id

        try:
            pgpMessage = PGPMessage.from_blob(message.content)
        except (ValueError, NotImplementedError) as e:
            log.warning(f'Decryption error for {msg_id}, not a PGPMessage: {e}')
            return

        key_list = [(session.account, self.private_key)] if self.private_key is not None else []
        key_list.extend(self.other_private_keys)

        error = None
        for (account, key) in key_list:
            try:
                decrypted_message = key.decrypt(pgpMessage)
            except (PGPDecryptionError, PGPError) as e:
                error = str(e)
                #log.debug(f'-- Decryption error for {msg_id} from {session.contact_uri.uri} with {account.id} : {error}')
                continue
            else:
                message.content = decrypted_message.message.decode() if isinstance(decrypted_message.message, bytearray) else decrypted_message.message.encode('latin1').decode()
                #log.info(f'Message decrypted: {msg_id}')
                notification_center.post_notification('PGPMessageDidDecrypt', sender=session, data=NotificationData(message=message, account=account))
                return

        log.debug(f'-- Decryption error for {msg_id} from {session.contact_uri.uri}: {error}')
        notification_center.post_notification('PGPMessageDidNotDecrypt', sender=session, data=NotificationData(message=message, error=error))

    @run_in_thread('pgp')
    def encrypt_file(self, filename, transfer_session):
        session = self.blink_session
        notification_center = NotificationCenter()
        pgp_message = PGPMessage.new(filename, file=True, compression=CompressionAlgorithm.Uncompressed)
        cipher = SymmetricKeyAlgorithm.AES256

        sessionkey = cipher.gen_key()
        encrypted_content = self.public_key.encrypt(pgp_message, cipher=cipher, sessionkey=sessionkey)
        encrypted_content = self.remote_public_key.encrypt(encrypted_content, cipher=cipher, sessionkey=sessionkey)
        del sessionkey
        notification_center.post_notification('PGPFileDidEncrypt', sender=session, data=NotificationData(filename=f'{filename}.asc', contents=encrypted_content))

    @run_in_thread('pgp')
    def decrypt_file(self, filename, transfer_session, must_open=False, id=None):
        session = self.blink_session
        notification_center = NotificationCenter()
        log.info(f'Decrypting {filename}')

        if self.private_key is None and len(self.other_private_keys) == 0:
            notification_center.post_notification('PGPFileDidNotDecrypt', sender=session, data=NotificationData(filename=filename, error="No private keys found"))
            return

        try:
            pgpMessage = PGPMessage.from_file(filename)
        except (ValueError, FileNotFoundError) as e:
            log.warning(f'Decryption failed for {filename}, this is not a PGP File, error: {e}')
            return

        key_list = [(session.account, self.private_key)] if self.private_key is not None else []
        key_list.extend(self.other_private_keys)

        error = None
        for (account, key) in key_list:
            try:
                decrypted_message = key.decrypt(pgpMessage)
            except (PGPDecryptionError, PGPError) as e:
                error = e
                log.debug(f'Decryption failed for {filename} with account key {account.id}, error: {error}')
                continue
            else:
                dir = os.path.dirname(filename)
                full_decrypted_filepath = os.path.join(dir, decrypted_message.filename)
                file_contents = decrypted_message.message if isinstance(decrypted_message.message, bytearray) else decrypted_message.message.encode('latin1')
                with open(full_decrypted_filepath, 'wb+') as output_file:
                    output_file.write(file_contents)
                correct_filepath = "%s/%s" % (dir, os.path.basename(filename)[:-4])
                if full_decrypted_filepath != correct_filepath:
                    # PGP messes up the filename, replacing _ with spaces
                    log.info(f"Renaming decrypted file to {correct_filepath}")
                    os.rename(full_decrypted_filepath, correct_filepath)

                log.info(f'Decrypted file saved: {correct_filepath}')
                unlink(filename)

                notification_center.post_notification('PGPFileDidDecrypt', sender=session, data=NotificationData(filename=full_decrypted_filepath, account=account, must_open=must_open, id=id))
                return

        log.warning(f'Decryption failed for {filename}, error: {error}')
        notification_center.post_notification('PGPFileDidNotDecrypt', sender=transfer_session, data=NotificationData(filename=filename, error=error))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_PGPKeysShouldReload(self, notification):
        if notification.sender is not self.blink_session:
            return

        # print('-- Reload PGP keys in stream')
        session = self.blink_session

        self.remote_public_key = self._load_key(session.remote_instance_id or str(session.contact_uri.uri), True)
        self.public_key = self._load_key(str(session.account.id))
        self.private_key = self._load_key(str(session.account.id), public_key=False)
        self.other_private_keys = []
        self._load_other_keys(session)
        if None not in [self.public_key, self.private_key] or self.can_decrypt_with_others:
            notification_center = NotificationCenter()
            notification_center.post_notification('MessageStreamPGPKeysDidLoad', sender=self)

    def _load_key(self, id, remote=False, public_key=True):
        settings = SIPSimpleSettings()
        loaded_key = None
        id = id.replace('/', '_')
        extension = 'pubkey'
        if not public_key:
            extension = 'privkey'

        directory = os.path.join(settings.chat.keys_directory.normalized, 'private')
        if remote:
            directory = settings.chat.keys_directory.normalized

        filename = os.path.join(directory, f'{id}.{extension}')
        if not os.path.exists(filename):
            return loaded_key

        try:
            loaded_key, _ = PGPKey.from_file(filename)
        except Exception as e:
            log.warning(f"Can't load PGP key: {str(e)}")

        return loaded_key

    def _load_pgp_keys(self):
        # print('-- Load PGP keys in stream')
        session = self.blink_session

        if self.remote_public_key is None:
            self.remote_public_key = self._load_key(session.remote_instance_id or str(session.contact_uri.uri), True)

        if self.public_key is None:
            self.public_key = self._load_key(str(session.account.id))

        if self.private_key is None:
            self.private_key = self._load_key(str(session.account.id), public_key=False)

        self.other_private_keys = []
        self._load_other_keys(session)

        if None not in [self.public_key, self.private_key] or self.can_decrypt_with_others:
            notification_center = NotificationCenter()
            notification_center.post_notification('MessageStreamPGPKeysDidLoad', sender=self)

    def _load_other_keys(self, session):
        account_manager = AccountManager()
        for account in (account for account in account_manager.iter_accounts() if account is not session.account and account.enabled):
            loaded_key = self._load_key(str(account.id), public_key=False)
            if loaded_key is None:
                continue
            self.other_private_keys.append((account, loaded_key))


OTRTransport.register(MessageStream)
