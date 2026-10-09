"""One shared FrameBufferVideoRenderer per remote video producer.

The SDK lets exactly one consumer attach to a RemoteVideoStream -- a second one
fails with "another consumer is already attached to this producer" -- so the
decoded frames of a call can only be read through the single renderer attached
to it, and whoever owns that renderer decides how long anybody else sees frames.

That owner used to be VideoSurface: the renderer was attached when a view got
the producer and detached again on every window hide, dock into the main
window and local/remote swap. Anything else that wanted the frames (the call
recorder) would have died with it.

This module owns the renderer instead. Subscribers come and go individually;
the renderer is created with the first subscriber of a producer and closed when
the last one goes away. The fan-out itself is the SDK's
(FrameBufferVideoRenderer.add_frame_handler).

Callbacks run on the pjsip video thread, in subscription order, and get the
same VideoFrame. They must return at once: this is the render path, and
anything slower than the frame interval stalls the video port.

The local camera does not need this -- VideoCamera has a tee and takes any
number of consumers -- so views of the camera keep their own renderer.
"""

from threading import RLock

from sipsimple.core import FrameBufferVideoRenderer

from blink.logging import ActivityLog


__all__ = ['subscribe', 'subscriber_count', 'Subscription']


# producer -> (renderer, set of live Subscription objects)
_sources = {}
_lock = RLock()


class Subscription(object):
    """A live frame subscription. release() ends it.

    The subscription, not the callback, is what is registered with the
    renderer: two reads of a widget's bound _handle_frame are equal but not
    identical, and the subscription gives release() one object to remove.
    """

    def __init__(self, producer, callback):
        self.producer = producer
        self.callback = callback

    def __call__(self, frame):
        callback = self.callback
        if callback is not None:
            callback(frame)

    @property
    def active(self):
        return self.callback is not None

    def release(self):
        if self.callback is None:
            return
        # dropped first, so a frame already in flight on the media thread stops here
        self.callback = None
        _release(self)


def subscribe(producer, callback):
    """Subscribe callback to producer's decoded frames.

    Returns a Subscription, or None when the producer cannot be rendered right
    now (the SDK is tearing it down, the device is closed, ...).
    """
    if producer is None or callback is None:
        return None

    subscription = Subscription(producer, callback)

    with _lock:
        source = _sources.get(producer)

        if source is None:
            try:
                renderer = FrameBufferVideoRenderer(subscription)
            except Exception as e:
                ActivityLog().warning(f'[video] Cannot create a renderer for producer 0x{id(producer):x} right now ({e})')
                return None
            try:
                renderer.producer = producer
            except Exception as e:
                ActivityLog().warning(f'[video] The SDK rejected producer 0x{id(producer):x} ({e})')
                try:
                    renderer.close()
                except Exception:
                    pass
                return None
            _sources[producer] = (renderer, {subscription})
            ActivityLog().debug(f'[video] Frame renderer opened on producer 0x{id(producer):x}')
            return subscription

        renderer, subscriptions = source
        try:
            renderer.add_frame_handler(subscription)
        except Exception as e:
            ActivityLog().warning(f'[video] Cannot subscribe to producer 0x{id(producer):x} ({e})')
            return None
        subscriptions.add(subscription)
        ActivityLog().debug(f'[video] Frame subscriber added on producer 0x{id(producer):x} ({len(subscriptions)} total)')
        return subscription


def subscriber_count(producer):
    with _lock:
        source = _sources.get(producer)
        return len(source[1]) if source is not None else 0


def _release(subscription):
    producer = subscription.producer
    subscription.producer = None

    with _lock:
        source = _sources.get(producer)
        if source is None:
            return
        renderer, subscriptions = source
        if subscription not in subscriptions:
            return
        subscriptions.discard(subscription)
        try:
            renderer.remove_frame_handler(subscription)
        except Exception as e:
            ActivityLog().debug(f'[video] remove_frame_handler ignored: {e}')
        if subscriptions:
            ActivityLog().debug(f'[video] Frame subscriber removed on producer 0x{id(producer):x} ({len(subscriptions)} left)')
            return
        del _sources[producer]

    # outside the lock: close() takes the SDK's video lock and can wait for the media thread to finish a frame
    try:
        renderer.close()
    except Exception as e:
        ActivityLog().debug(f'[video] Frame renderer close ignored: {e}')
    else:
        ActivityLog().debug(f'[video] Frame renderer closed on producer 0x{id(producer):x}')
