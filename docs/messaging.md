
Messaging
=========

Blink supports two types of media for handling text messages.

1. SIP Message method (asynchronous)
2. MSRP chat (in real-time)


Asynchronous messaging
======================

To send messages asynchronously, which may be delivered either in real time
or at a later time, just type the message to the selected contact.  These
messages will be delivered using SIP Message method. If the message failed
to be delivered becuase of network conditions, the message will be resent at
a later time. 


Encryption
==========

If there is a PGP public key for the SIP address of the recipient, the
messages will be encrypted using it.  As an alternative method of
encryption, one can start an end-to-end OTR encrypted session between two
end-points, in this case PGP encryption will be disabled and messages will
only be delivered to the end-point that accepted the OTR session.

MSRP sessions are encrypted using TLS and OTR protocol can be used for
end-to-end encryption.


Offline messaging
=================

For offline messaging and replication of messages between multiple SIP
devices, and discovery of PGP public keys, one must install and configure
SylkServer.

Blink asynchronous messaging features are compatible with other SIP clients
that implement Sylk Server messaging API.  Sylk Desktop and Sylk Mobile have
been tested and are known to support the same messaging API as Blink.  All
messages exchanged with one SIP client are replicated to all other SIP
clients configured with the same SIP account.


Server setup
=============

The SIP Proxy must fork a copy of all SIP messages to SylkServer.  When
properly configured, SylkServer will respond to SIP messages sent by Blink
with the following content types:

  * application/sylk-api-token (will auto discover the URL and index for offline storage)
  * application/sylk-api-pgp-key-lookup (will return the OpenPGP key if exists)
  * application/sylk-api-message-remove for message removal

All Sylk server settings are auto-discovered by Blink.

To replicate messages from multiple devices they all must use the same
private PGP key.  The import PGP panel is presented at start of messaging
sessions for each account.  The key can be exported from another device that
implement the same API (see Chat menu item Export PGP private key) or can be
copied manually to ~/.blink/keys/private configuration folder with the
following convention:

 * user@domain.privkey
 * user@domain.pubkey

The public keys of the recipients are looked up in SylkServer at the start
of a messaging session.  If a key is found, it will be returned as SIP
message with a special content type recognized by Blink and saved in a file
e.g. ~/.blink/keys/user@domain.pubkey


Messaging group
===============

By default, every contact with whom messages were exchanged will appear in a
the Messages group.  This fucntionality can be turned off in
Preferences -> Advanced -> Interface.


Multiple accounts
=================

When multiple accounts are present, you can chose which account to be used
for outgoing messages by selecting it from the drop down box present in the
bottom left of the Messages window.


MSRP chat 
=========

To start an MSRP session based chat open the Chat window, click on the
session menu button and select Start MSRP Chat option or right click a
contact and select the same option.  Once established, the session using
MSRP media uses TLS transport and end-to-end encryption using OTR is by
default enabled.

