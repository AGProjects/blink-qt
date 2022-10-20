Supported features between the different clients
================================================

|                       Feature |       Blink Qt      |    Blink Cocoa   | Sylk Desktop | Sylk (Chrome based) | Sylk (Firefox) | Sylk (Safari) | Sylk (mobile) |
|------------------------------:|:-------------------:|:----------------:|:------------:|:-------------------:|:--------------:|:-------------:|:-------------:|
|               **Audio Calls** |                     |                  |              |                     |                |               |               |
|                          g711 |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|                          g722 |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|                          opus |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|                          zRTP |          ✓          |         ✓        |       ✕      |          ✕          |        ✕       |       ✕       |       ✕       |
|               **Video Calls** |                     |                  |              |                     |                |               |               |
|                          h264 |      Not tested     |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|                           VP8 |          ✓*         |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|                           VP9 |          ✓*         |         ✓        |       ✓      |          ✓          |        ✓       |       ✕       |       ✓       |
|             **Screensharing** | Only to Blink Cocoa | Only to Blink QT |   Is video   |       Is video      |    Is video    |    Is video   |    Is video   |
|                  **Messages** |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
| **PGP encryption/decryption** |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|            **PGP key lookup** |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|     **PGP key import/export** |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|                           OTR |          ✓          |         ✓        |       ✕      |          ✕          |        ✕       |       ✕       |       ✕       |
|                   **History** |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|       **History replication** |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |
|                      **IMDN** |          ✓          |         ✓        |       ✓      |          ✓          |        ✓       |       ✓       |       ✓       |

* Could only see own outgoing video on both blink-qt and other devices






