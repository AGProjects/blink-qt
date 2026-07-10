import os

# Used by MacOS app, see macos/README

if __name__ == '__main__':
    from blink import Blink
    blink = Blink()
    blink.run()
    os._exit(0)
