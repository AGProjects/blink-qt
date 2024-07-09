
Messaging notes
===============

Blink support two types of media for messaging.

1. SIP Message method (asynchronous messaging)
2. Session based chat using MSRP protocol (real-time chat sessions)


Asynchronous messaging
======================

To send messages asynchronously, which may be delivered either in real time
of at a later time, just type the message to the selected contact.  These
messages will be delivered using SIP Message method.  By default, encryption
using PGP mechanism is used, if there is a public key present for the SIP
address of the recipient, the messages will be automatically encrypted using
it.  As an alternative, one can start an end-to-end OTR encrypted session
between two end-points, in this case PGP encryption will be disabled and
messages will only be delivered to the end-point that accepted the OTR
session.

To replicate messages between multiple SIP devices, discover PGP public keys
and receive messages while offline, one must install SylkServer and
configure it with Cassandra storage backend.

Blink asynchronous messaging features are compatible with other SIP clients
that implement Sylk Server messaging API.  Sylk Desktop and Sylk Mobile have
been tested and are known to support the same messaging API as Blink.  All
messages exchanged with one SIP client are replicated to all other SIP
clients configured with the same SIP account.

The SIP Proxy must fork a copy of all SIP messages to SylkServer.  When
properly configured, SylkServer will respond to SIP messages sent by Blink
with the following content types:

  * application/sylk-api-token (will auto discover the URL and index for offline storage)
  * application/sylk-api-pgp-key-lookup (will return the OpenPGP key if exists)
  * application/sylk-message-remove for message removal
  * application/sylk-conversation-remove for removal of all messages with a contact
  * application/sylk-conversation-read to confrim read of all messages
  * application/sylk-file-transfer containing MSRP URL for file download

When using SylkServer, all server settings are auto-discovered by Blink.

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


Real-time chat 
==============

To start an MSRP session based chat open the Chat window, click on the
session menu button and select Start MSRP Chat option or right click a
contact and select the same option.  Once established, the session using
MSRP media uses TLS transport and end-to-end encryption using OTR is by
default enabled.

