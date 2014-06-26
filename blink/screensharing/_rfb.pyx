# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

__all__ = ['RFBClient', 'RFBClientError']

from sip import voidptr
from PyQt4.QtCore import QThread
from PyQt4.QtGui  import QImage

from application.notification import NotificationCenter, NotificationData

from libc.stdint cimport uint8_t, uint16_t, uint32_t
from libc.stdlib cimport calloc, malloc, free
from libc.string cimport memcpy, strlen


# external declarations
#

cdef extern from "stdarg.h":
    ctypedef struct va_list:
        pass
    void va_start(va_list, void *arg)
    void va_end(va_list)


cdef extern from "Python.h":
    object PyString_FromStringAndSize(const char *u, Py_ssize_t size)
    object PyUnicode_FromStringAndSize(const char *u, Py_ssize_t size)
    int PyOS_vsnprintf(char *buf, size_t size, const char *format, va_list va)


cdef extern from "rfb/rfbclient.h":
    ctypedef int rfbBool

    # forward declarations
    ctypedef struct _rfbClient

    enum: rfbCredentialTypeX509=1, rfbCredentialTypeUser=2

    ctypedef struct UserCredential:
        char *username
        char *password

    ctypedef union rfbCredential:
        #X509Credential x509Credential
        UserCredential userCredential

    ctypedef struct AppData:
        const char* encodingsString
        int compressLevel
        int qualityLevel
        int requestedDepth
        rfbBool enableJPEG
        rfbBool useRemoteCursor

    ctypedef struct rfbPixelFormat:
        uint8_t  bitsPerPixel
        uint8_t  depth
        uint16_t redMax
        uint16_t greenMax
        uint16_t blueMax
        uint8_t  redShift
        uint8_t  greenShift
        uint8_t  blueShift

    ctypedef struct rfbClientData:
        void *data

    ctypedef struct UpdateRect:
        int x, y, w, h

    ctypedef struct rfbServerInitMsg:
        uint16_t framebufferWidth
        uint16_t framebufferHeight
        rfbPixelFormat format # the server's preferred pixel format

    # callbacks
    ctypedef void (*rfbClientLogProc)(const char *format, ...)

    ctypedef rfbBool (*MallocFrameBufferProc)(_rfbClient *client) nogil
    ctypedef void (*GotFrameBufferUpdateProc)(_rfbClient *client, int x, int y, int w, int h) nogil
    ctypedef char* (*GetPasswordProc)(_rfbClient *client) nogil
    ctypedef rfbCredential* (*GetCredentialProc)(_rfbClient *client, int credentialType) nogil
    ctypedef void (*GotXCutTextProc)(_rfbClient *client, const char *text, int textlen) nogil
    ctypedef void (*GotCursorShapeProc)(_rfbClient *client, int xhot, int yhot, int width, int height, int bytesPerPixel)
    ctypedef rfbBool (*HandleCursorPosProc)(_rfbClient *client, int x, int y)
    #ctypedef void (*BellProc)(_rfbClient *client)

    ctypedef struct _rfbClient:
        char *serverHost
        int   serverPort

        int sock
        int width, height
        uint8_t *frameBuffer
        UpdateRect updateRect

        AppData appData
        rfbPixelFormat format
        rfbServerInitMsg si
        rfbClientData *clientData

        # cursor
        uint8_t *rcSource
        uint8_t *rcMask

        rfbBool canHandleNewFBSize

        # callbacks
        MallocFrameBufferProc MallocFrameBuffer
        GotFrameBufferUpdateProc GotFrameBufferUpdate
        GotCursorShapeProc GotCursorShape
        HandleCursorPosProc HandleCursorPos
        GetPasswordProc GetPassword     # the pointer returned will be freed after use!
        GetCredentialProc GetCredential # the pointer returned will be freed after use!
        GotXCutTextProc GotXCutText
        #BellProc Bell

    ctypedef _rfbClient rfbClient

    # functions
    rfbClient* rfbGetClient(int bitsPerSample, int samplesPerPixel, int bytesPerPixel) nogil
    void rfbClientCleanup(rfbClient *client) nogil

    rfbBool ConnectToRFBServer(rfbClient *client, const char *hostname, int port) nogil
    rfbBool InitialiseRFBConnection(rfbClient *client) nogil
    rfbBool SetFormatAndEncodings(rfbClient *client) nogil
    rfbBool SendFramebufferUpdateRequest(rfbClient *client, int x, int y, int w, int h, rfbBool incremental) nogil

    rfbBool SendPointerEvent(rfbClient *client, int x, int y, int buttonMask) nogil
    rfbBool SendKeyEvent(rfbClient *client, uint32_t key, rfbBool down) nogil
    rfbBool SendClientCutText(rfbClient *client, char *str, int len) nogil

    int     WaitForMessage(rfbClient *client, unsigned int usecs) nogil
    rfbBool HandleRFBServerMessage(rfbClient *client) nogil


# Provide our own strdup implementation because Windows is ... well, Windows
#
cdef char* strdup(const char *string):
    cdef size_t len = strlen(string) + 1
    cdef void *copy = malloc(len)
    return <char*> memcpy(copy, string, len) if copy else NULL


# RFB client implementation
#

class RFBClientError(Exception): pass


cdef class RFBClient:
    cdef rfbClient *client
    cdef uint8_t *framebuffer
    cdef unsigned int framebuffer_size
    cdef int connected

    cdef readonly object parent
    cdef readonly object image

    def __cinit__(self, parent, *args, **kw):
        cdef char *server_host = NULL
        cdef rfbClientData *client_data = NULL

        try:
            with nogil:
                self.client = rfbGetClient(8, 3, 4) # 24 bit color depth in 32 bits per pixel. Will change color depth and bpp later if needed.
            server_host = strdup(parent.host)
            client_data = <rfbClientData*> calloc(1, sizeof(rfbClientData))
            if not server_host or not client_data or not self.client:
                raise MemoryError("could not allocate RFB client")
            free(self.client.serverHost)
            client_data.data = <void*>self
        except:
            free(server_host)
            free(client_data)
            raise

        self.client.clientData = client_data
        self.client.serverHost = server_host
        self.client.serverPort = parent.port
        self.client.canHandleNewFBSize = True
        self.client.appData.useRemoteCursor = False
        self.client.MallocFrameBuffer = _malloc_framebuffer_callback
        self.client.GotFrameBufferUpdate = _update_framebuffer_callback
        self.client.GotCursorShape = _update_cursor_callback
        self.client.HandleCursorPos = _update_cursor_position_callback
        self.client.GetPassword = _get_password_callback
        self.client.GetCredential = _get_credentials_callback
        self.client.GotXCutText = _text_cut_callback
        self.connected = False

    def __init__(self, parent, *args, **kw):
        self.parent = parent
        self.image = QImage()

    def __dealloc__(self):
        if self.client:
            with nogil:
                rfbClientCleanup(self.client)
        if self.framebuffer:
            if not self.image.isNull():
                self.image.setPixel(0, 0, self.image.pixel(0, 0)) # detach the image from the framebuffer we're about to release to avoid race conditions when painting in the GUI thread
            free(self.framebuffer)

    property framebuffer:
        def __get__(self):
            return voidptr(<long>self.framebuffer, size=self.framebuffer_size)

    property server_depth:
        def __get__(self):
            return self.client.si.format.depth or None

    property socket:
        def __get__(self):
            return self.client.sock

    def configure(self):
        depth = self.parent.settings.depth or self.server_depth
        format_changed = depth != self.client.format.depth
        if depth == 8:
            self.client.format.depth = 8
            self.client.format.bitsPerPixel = 8
            self.client.format.redShift = 0
            self.client.format.greenShift = 3
            self.client.format.blueShift = 6
            self.client.format.redMax = 7
            self.client.format.greenMax = 7
            self.client.format.blueMax = 3
        elif depth == 16:
            self.client.format.depth = 16
            self.client.format.bitsPerPixel = 16
            self.client.format.redShift = 11
            self.client.format.greenShift = 5
            self.client.format.blueShift = 0
            self.client.format.redMax = 0x1f
            self.client.format.greenMax = 0x3f
            self.client.format.blueMax = 0x1f
        elif depth in (24, 32):
            self.client.format.depth = depth
            self.client.format.bitsPerPixel = 32
            self.client.format.redShift = 16
            self.client.format.greenShift = 8
            self.client.format.blueShift = 0
            self.client.format.redMax = 0xff
            self.client.format.greenMax = 0xff
            self.client.format.blueMax = 0xff
        self.client.appData.requestedDepth = self.client.format.depth
        self.client.appData.enableJPEG = bool(self.client.format.bitsPerPixel != 8)
        self.client.appData.encodingsString = self.parent.settings.encodings
        self.client.appData.compressLevel = self.parent.settings.compression
        self.client.appData.qualityLevel = self.parent.settings.quality
        if self.connected:
            with nogil:
                result = SetFormatAndEncodings(self.client)
            if not result:
                raise RFBClientError("failed to set format and encodings")
            with nogil:
                result = SendFramebufferUpdateRequest(self.client, self.client.updateRect.x, self.client.updateRect.y, self.client.updateRect.w, self.client.updateRect.h, False)
            if not result:
                raise RFBClientError("failed to refresh screen after changing format and encodings")
            if format_changed:
                if not self.framebuffer:
                    self.image = QImage(self.client.width, self.client.height, QImage.Format_Invalid)
                elif self.client.format.bitsPerPixel == 32:
                    self.image = QImage(voidptr(<long>self.framebuffer, size=self.framebuffer_size), self.client.width, self.client.height, QImage.Format_RGB32)
                elif self.client.format.bitsPerPixel == 16:
                    self.image = QImage(voidptr(<long>self.framebuffer, size=self.framebuffer_size), self.client.width, self.client.height, QImage.Format_RGB16)
                elif self.client.format.bitsPerPixel == 8:
                    self.image = QImage(voidptr(<long>self.framebuffer, size=self.framebuffer_size), self.client.width, self.client.height, QImage.Format_Indexed8)
                else:
                    self.image = QImage(self.client.width, self.client.height, QImage.Format_Invalid)

    def connect(self):
        cdef rfbBool result

        if self.connected:
            return

        with nogil:
            result = ConnectToRFBServer(self.client, self.client.serverHost, self.client.serverPort)
        if not result:
            raise RFBClientError("could not connect")
        with nogil:
            result = InitialiseRFBConnection(self.client)
        if not result:
            raise RFBClientError("could not initialise connection")

        self.client.width  = self.client.si.framebufferWidth
        self.client.height = self.client.si.framebufferHeight
        self.client.updateRect.x = 0
        self.client.updateRect.y = 0
        self.client.updateRect.w = self.client.width
        self.client.updateRect.h = self.client.height

        self.configure()

        self._malloc_framebuffer_callback()
        if not self.framebuffer:
            raise RFBClientError("could not allocate framebuffer memory")

        with nogil:
            result = SetFormatAndEncodings(self.client)
        if not result:
            raise RFBClientError("could not set format and encodings")
        with nogil:
            result = SendFramebufferUpdateRequest(self.client, self.client.updateRect.x, self.client.updateRect.y, self.client.updateRect.w, self.client.updateRect.h, False)
        if not result:
            raise RFBClientError("could not request framebuffer update")

        self.connected = True

    def handle_server_message(self):
        cdef int result
        with nogil:
            result = HandleRFBServerMessage(self.client)
        if not result:
            raise RFBClientError("could not process server message")

    def send_key_event(self, uint32_t key, rfbBool down):
        cdef int result
        if not self.connected:
            return
        with nogil:
            result = SendKeyEvent(self.client, key, down)
        if not result:
            raise RFBClientError("could not send key event")

    def send_pointer_event(self, int x, int y, int button_mask):
        cdef int result
        if not self.connected:
            return
        with nogil:
            result = SendPointerEvent(self.client, x, y, button_mask)
        if not result:
            raise RFBClientError("could not send pointer event")

    def send_client_cut_text(self, unicode text):
        cdef int result, strlen
        cdef bytes text_utf8
        cdef char *string

        if not self.connected:
            return

        text_utf8 = text.encode('utf8')
        string = text_utf8
        strlen = len(text_utf8)

        with nogil:
            result = SendClientCutText(self.client, string, strlen)
        if not result:
            raise RFBClientError("could not send client cut text")

    cdef rfbBool _malloc_framebuffer_callback(self):
        if self.framebuffer:
            if not self.image.isNull():
                self.image.setPixel(0, 0, self.image.pixel(0, 0)) # detach the image from the framebuffer we're about to release to avoid race conditions when painting in the GUI thread
            free(self.framebuffer)
        self.framebuffer_size = self.client.width * self.client.height * 4 # always allocate a framebuffer that can hold 32bpp so we can change the depth mid-session without reallocating
        self.framebuffer = <uint8_t*> malloc(self.framebuffer_size)
        self.client.frameBuffer = self.framebuffer
        if not self.framebuffer:
            self.image = QImage(self.client.width, self.client.height, QImage.Format_Invalid)
        elif self.client.format.bitsPerPixel == 32:
            self.image = QImage(voidptr(<long>self.framebuffer, size=self.framebuffer_size), self.client.width, self.client.height, QImage.Format_RGB32)
        elif self.client.format.bitsPerPixel == 16:
            self.image = QImage(voidptr(<long>self.framebuffer, size=self.framebuffer_size), self.client.width, self.client.height, QImage.Format_RGB16)
        elif self.client.format.bitsPerPixel == 8:
            self.image = QImage(voidptr(<long>self.framebuffer, size=self.framebuffer_size), self.client.width, self.client.height, QImage.Format_Indexed8)
        else:
            self.image = QImage(self.client.width, self.client.height, QImage.Format_Invalid)
        self.parent.imageSizeChanged.emit(self.image.size())
        return bool(<long>self.framebuffer)

    cdef void _update_framebuffer_callback(self, int x, int y, int w, int h):
        self.parent.imageChanged.emit(x, y, w, h)

    cdef void _update_cursor_callback(self, int xhot, int yhot, int width, int height, int bytes_per_pixel):
        image = PyString_FromStringAndSize(<const char*>self.client.rcSource, width*height*bytes_per_pixel)
        mask  = PyString_FromStringAndSize(<const char*>self.client.rcMask, width*height)
        #print "-- update cursor shape:", xhot, yhot, width, height, bytes_per_pixel, repr(image), repr(mask)

    cdef rfbBool _update_cursor_position_callback(self, int x, int y):
        #print "-- update cursor position to:", x, y
        return True

    cdef char* _get_password_callback(self):
        self.parent.passwordRequested.emit(False)
        return NULL if self.parent.password is None else strdup(<bytes>self.parent.password.encode('utf8'))

    cdef rfbCredential* _get_credentials_callback(self, int credentials_type):
        cdef rfbCredential *credential = NULL

        if credentials_type == rfbCredentialTypeUser:
            self.parent.passwordRequested.emit(True)
            if self.parent.username is not None is not self.parent.password:
                credential = <rfbCredential*> malloc(sizeof(rfbCredential))
                credential.userCredential.username = strdup(<bytes>self.parent.username.encode('utf8'))
                credential.userCredential.password = strdup(<bytes>self.parent.password.encode('utf8'))

        return credential

    cdef void _text_cut_callback(self, const char *text, int length):
        cut_text = PyUnicode_FromStringAndSize(text, length)
        if cut_text:
            self.parent.textCut.emit(cut_text)


# callbacks
#
cdef rfbBool _malloc_framebuffer_callback(rfbClient *client) with gil:
    instance = <RFBClient> client.clientData.data
    return instance._malloc_framebuffer_callback()

cdef void _update_framebuffer_callback(rfbClient *client, int x, int y, int w, int h) with gil:
    instance = <RFBClient> client.clientData.data
    instance._update_framebuffer_callback(x, y, w, h)

cdef void _update_cursor_callback(rfbClient *client, int xhot, int yhot, int width, int height, int bytes_per_pixel) with gil:
    instance = <RFBClient> client.clientData.data
    instance._update_cursor_callback(xhot, yhot, width, height, bytes_per_pixel)

cdef rfbBool _update_cursor_position_callback(rfbClient *client, int x, int y) with gil:
    instance = <RFBClient> client.clientData.data
    return instance._update_cursor_position_callback(x, y)

cdef char* _get_password_callback(rfbClient *client) with gil:
    instance = <RFBClient> client.clientData.data
    return instance._get_password_callback()

cdef rfbCredential* _get_credentials_callback(rfbClient *client, int credentials_type) with gil:
    instance = <RFBClient> client.clientData.data
    return instance._get_credentials_callback(credentials_type)

cdef void _text_cut_callback(rfbClient *client, const char *text, int length) with gil:
    instance = <RFBClient> client.clientData.data
    instance._text_cut_callback(text, length)


cdef void _rfb_client_log(const char *format, ...) with gil:
    cdef char buffer[512]
    cdef va_list args

    va_start(args, format)
    PyOS_vsnprintf(buffer, sizeof(buffer), format, args)
    va_end(args)

    message = (<bytes>buffer).rstrip()

    NotificationCenter().post_notification('RFBClientLog', data=NotificationData(message=message.decode('utf8'), thread=QThread.currentThread()))


cdef extern rfbClientLogProc rfbClientLog = _rfb_client_log
cdef extern rfbClientLogProc rfbClientErr = _rfb_client_log


