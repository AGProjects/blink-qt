import os

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.system import makedirs

from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import IMediaStream, MediaStreamType, UnknownStreamError
from sipsimple.threading import run_in_thread
from sipsimple.threading.green import run_in_green_thread

from zope.interface import implementer

from pgpy import PGPKey, PGPUID, PGPMessage
from pgpy.errors import PGPError, PGPDecryptionError
from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm, CompressionAlgorithm

from blink.util import run_in_gui_thread


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
    def can_encrypt(self):
        return self.private_key is not None and self.remote_public_key is not None

    @property
    def can_decrypt(self):
        return self.private_key is not None

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

    def enable_pgp(self):
        self._load_pgp_keys()

    def encrypt(self, content):
        session = self.blink_session
        # print('-- Encrypting message')

        stream = session.fake_streams.get('messages')
        pgp_message = PGPMessage.new(content, compression=CompressionAlgorithm.Uncompressed)
        cipher = SymmetricKeyAlgorithm.AES256

        sessionkey = cipher.gen_key()
        encrypted_content = stream.public_key.encrypt(pgp_message, cipher=cipher, sessionkey=sessionkey)
        encrypted_content = stream.remote_public_key.encrypt(encrypted_content, cipher=cipher, sessionkey=sessionkey)
        del sessionkey
        return str(encrypted_content)

    @run_in_thread('pgp')
    def decrypt(self, message):
        session = self.blink_session
        notification_center = NotificationCenter()

        if self.private_key is None and len(self.other_private_keys) == 0:
            notification_center.post_notification('PGPMessageDidNotDecrypt', sender=session, data=NotificationData(message=message))

        try:
            msg_id = message.message_id
        except AttributeError:
            msg_id = message.id

        # print(f'-- Decrypting message {msg_id}')
        try:
            pgpMessage = PGPMessage.from_blob(message.content)
        except (ValueError) as e:
            log.warning(f'Decryption failed for {msg_id}, this is not a PGPMessage, error: {e}')
            return

        key_list = [(session.account.id, self.private_key)] if self.private_key is not None else []
        key_list.extend(self.other_private_keys)

        error = None
        for (account, key) in key_list:
            try:
                decrypted_message = key.decrypt(pgpMessage)
            except (PGPDecryptionError, PGPError) as error:
                log.debug(f'-- Decryption failed for {msg_id} with account key {account}, error: {error}')
                continue
            else:
                message.content = decrypted_message.message.decode() if isinstance(decrypted_message.message, bytearray) else decrypted_message.message
                notification_center.post_notification('PGPMessageDidDecrypt', sender=session, data=NotificationData(message=message, account=account))
                return

        log.warning(f'-- Decryption failed for {msg_id}, error: {error}')
        notification_center.post_notification('PGPMessageDidNotDecrypt', sender=session, data=NotificationData(message=message, error=error))

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def PGPKeysShouldReload(self, notification):
        if notification.sender is not self.blink_session:
            return

        # print('-- Reload PGP keys in stream')
        session = self.blink_session

        self.remote_public_key = self._load_key(str(session.contact_uri.uri), True)
        self.public_key = self._load_key(str(session.account.id))
        self.private_key = self._load_key(str(session.account.id), public_key=False)
        self._load_other_keys(session)

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
            self.remote_public_key = self._load_key(str(session.contact_uri.uri), True)

        if self.public_key is None:
            self.public_key = self._load_key(str(session.account.id))

        if self.private_key is None:
            self.private_key = self._load_key(str(session.account.id), public_key=False)

        if None not in [self.remote_public_key, self.public_key, self.private_key]:
            notification_center = NotificationCenter()
            notification_center.post_notification('MessageStreamPGPKeysDidLoad', sender=self)
        self._load_other_keys(session)

    def _load_other_keys(self, session):
        account_manager = AccountManager()
        for account in (account for account in account_manager.iter_accounts() if account is not session.account and account.enabled):
            loaded_key = self._load_key(str(account.id), public_key=False)
            if loaded_key is None:
                continue
            self.other_private_keys.append((account.id, loaded_key))

