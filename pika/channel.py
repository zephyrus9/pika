import pika.spec as spec
import pika.codec as codec
import pika.event as event
from pika.exceptions import *

class ChannelHandler:
    def __init__(self, connection, channel_number = None):
        self.connection = connection
        self.frame_handler = self._handle_method
        self.channel_close = None
        self.async_map = {}
        self.reply_map = {}

        self.channel_state_change_event = event.Event()

        if channel_number is None:
            self.channel_number = connection._next_channel_number()
        else:
            self.channel_number = channel_number
        connection._set_channel(self.channel_number, self)

    def _async_channel_close(self, method_frame, header_frame, body):
        self._set_channel_close(method_frame.method)
        self.connection.send_method(self.channel_number, spec.Channel.CloseOk())

    def _ensure(self):
        if self.channel_close:
            raise ChannelClosed(self.channel_close)
        return self

    def _set_channel_close(self, c):
        if not self.channel_close:
            self.channel_close = c
            self.connection.reset_channel(self.channel_number)
            self.channel_state_change_event.fire(self, False)

    def addStateChangeHandler(self, handler, key = None):
        self.channel_state_change_event.addHandler(handler, key)
        handler(self, not self.channel_close)

    def wait_for_reply(self, acceptable_replies):
        if not acceptable_replies:
            # One-way.
            return
        reply = [None]
        def set_reply(r):
            reply[0] = r
        for possibility in acceptable_replies:
            if possibility in self.reply_map:
                raise RecursiveOperationDetected(possibility.NAME)
            self.reply_map[possibility] = set_reply
        while True:
            self._ensure()
            self.connection.drain_events()
            if reply[0]: return reply[0]

    def _handle_async(self, method_frame, header_frame, body):
        method = method_frame.method
        methodClass = method.__class__

        if methodClass in self.reply_map:
            if header_frame is not None:
                method._set_content(header_frame.properties, body)
            handler = self.reply_map[methodClass]
            del self.reply_map[methodClass]
            handler(method)
        elif methodClass in self.async_map:
            self.async_map[methodClass](method_frame, header_frame, body)
        else:
            self.connection.close(spec.NOT_IMPLEMENTED,
                                  'Pika: method not implemented: ' + methodClass.NAME)

    def _handle_method(self, frame):
        if not isinstance(frame, codec.FrameMethod):
            raise UnexpectedFrameError(frame)
        if spec.has_content(frame.method.INDEX):
            self.frame_handler = self._make_header_handler(frame)
        else:
            self._handle_async(frame, None, None)

    def _make_header_handler(self, method_frame):
        def handler(header_frame):
            if not isinstance(header_frame, codec.FrameHeader):
                raise UnexpectedFrameError(header_frame)
            self._install_body_handler(method_frame, header_frame)
        return handler

    def _install_body_handler(self, method_frame, header_frame):
        seen_so_far = [0]
        body_fragments = []

        def handler(body_frame):
            if not isinstance(body_frame, codec.FrameBody):
                raise UnexpectedFrameError(body_frame)
            fragment = body_frame.fragment
            seen_so_far[0] = seen_so_far[0] + len(fragment)
            body_fragments.append(fragment)
            if seen_so_far[0] == header_frame.body_size:
                finish()
            elif seen_so_far[0] > header_frame.body_size:
                raise BodyTooLongError()
            else:
                pass
        def finish():
            self.frame_handler = self._handle_method
            self._handle_async(method_frame, header_frame, ''.join(body_fragments))

        if header_frame.body_size == 0:
            finish()
        else:
            self.frame_handler = handler

    def _rpc(self, method, acceptable_replies):
        self._ensure()
        return self.connection._rpc(self.channel_number, method, acceptable_replies)

class Channel(spec.DriverMixin):
    def __init__(self, handler):
        self.handler = handler
        self.callbacks = {}
        self.next_consumer_tag = 0

        handler.async_map[spec.Channel.Close] = handler._async_channel_close

        handler.async_map[spec.Basic.Deliver] = self._async_basic_deliver
        handler.async_map[spec.Basic.Return] = self._async_basic_return
        handler.async_map[spec.Channel.Flow] = self._async_channel_flow

        self.handler._rpc(spec.Channel.Open(), [spec.Channel.OpenOk])

    def addStateChangeHandler(self, handler, key = None):
        self.handler.addStateChangeHandler(handler, key)

    def _async_basic_deliver(self, method_frame, header_frame, body):
        self.callbacks[method_frame.method.consumer_tag](self,
                                                         method_frame.method,
                                                         header_frame.properties,
                                                         body)

    def _async_basic_return(self, method_frame, header_frame, body):
        raise NotImplementedError("Basic.Return")

    def _async_channel_flow(self, method_frame, header_frame, body):
        raise NotImplementedError("Channel.Flow")

    def close(self, code = 0, text = 'Normal close'):
        c = spec.Channel.Close(reply_code = code,
                               reply_text = text,
                               class_id = 0,
                               method_id = 0)
        self.handler._rpc(c, [spec.Channel.CloseOk])
        self.handler._set_channel_close(c)

    def basic_publish(self, exchange, routing_key, body, properties = None, mandatory = False, immediate = False):
        properties = properties or spec.BasicProperties()
        self.handler.connection.send_method(self.handler.channel_number,
                                            spec.Basic.Publish(exchange = exchange,
                                                               routing_key = routing_key,
                                                               mandatory = mandatory,
                                                               immediate = immediate),
                                            (properties, body))

    def basic_consume(self, consumer, queue = '', no_ack = False, exclusive = False, consumer_tag = None):
        tag = consumer_tag
        if not tag:
            tag = 'ctag' + str(self.next_consumer_tag)
            self.next_consumer_tag += 1

        if tag in self.callbacks:
            raise DuplicateConsumerTag(tag)

        self.callbacks[tag] = consumer
        return self.handler._rpc(spec.Basic.Consume(queue = queue,
                                                    consumer_tag = tag,
                                                    no_ack = no_ack,
                                                    exclusive = exclusive),
                                 [spec.Basic.ConsumeOk]).consumer_tag

    def basic_cancel(self, consumer_tag):
        if not consumer_tag in self.callbacks:
            raise UnknownConsumerTag(consumer_tag)

        self.handler._rpc(spec.Basic.Cancel(consumer_tag = consumer_tag),
                          [spec.Basic.CancelOk])
        del self.callbacks[consumer_tag]