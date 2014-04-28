import logging
import threading
import time
import gst
import gobject; gobject.threads_init()
import socket
import struct


class SlimAudioBuffer(threading.Thread):
    """SlimAudio buffer manage connection with server to retrieve stream content before playback"""
    SOCKET_DISCONNECTED = 0
    SOCKET_CONNECTED = 1
    SOCKET_READ_HEADER = 2
    SOCKET_ERROR = 3
    SOCKET_TIMEOUT = 4
    SOCKET_UNREACHABLE = 5
    
    BUFFER_EMPTY = 0
    BUFFER_READY = 1
    BUFFER_LOW = 2
    BUFFER_FULL = 3
    
    def __init__(self, status_buffer_callback=None, status_socket_callback=None):
        #init
        threading.Thread.__init__(self)
        self.running = True
        self.logger = logging.getLogger("SlimAudioBuffer")

        #members
        self.__http_header = None
        self.socket = None
        self.buffer_lock = threading.Lock()
        self.__buffer_size = 0
        self.status_socket = SlimAudioBuffer.SOCKET_DISCONNECTED
        self.status_buffer = SlimAudioBuffer.BUFFER_EMPTY
        self.__bytes_received = 0
        self.__bytes_read = 0
        self.__fullness = 0
        self.__write_ptr = 0
        self.__read_ptr = 0
        self.__buffer = None
        self.__mv = None
        
        #callbacks
        self.status_buffer_callback = status_buffer_callback
        self.status_socket_callback = status_socket_callback
        
    def connect(self, server_ip, server_port, http_header):
        """connect to audio stream
           @param server_ip : server ip
           @param server_port : server port
           @param http_header : received http header from strm-s server command"""
        try:
            #prepare socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setblocking(1)
            self.logger.debug('Connecting to %s:%d...' % (server_ip, server_port))
            self.socket.connect((server_ip, server_port))
            
            #send received header
            self.socket.sendall(http_header)
            
            self.status_socket = SlimAudioBuffer.SOCKET_READ_HEADER
            self.logger.debug('Connected!')
        except socket.error, e:
            #failed to connect
            self.status_socket = SlimAudioBuffer.SOCKET_UNREACHABLE
            self.logger.critical('Connection failed: %s' % e)
            return False
        except socket.timeout, e:
            #unable to connect
            self.status_socket = SlimAudioBuffer.SOCKET_TIMEOUT
            self.logger.critical('Connection timeout: %s' % e)
            return False
        return True
        
    def disconnect(self):
        """Disconnect audio stream"""
        if self.status_socket==SlimAudioBuffer.SOCKET_CONNECTED or self.status_socket==SlimAudioBuffer.SOCKET_READ_HEADER:
            self.logger.debug('Disconnected from audio stream')
            self.status_socket = SlimAudioBuffer.SOCKET_DISCONNECTED
            if self.socket:
                self.socket.close()
            
    def reset(self, buffer_size):
        """reset audio stream"""
        self.logger.debug('Reset')
        #disconnect socket
        self.disconnect()
        
        #reset buffer
        self.__buffer_size = buffer_size
        self.__buffer = bytearray(self.__buffer_size)
        self.__mv = memoryview(self.__buffer)
        
        #reset members
        self.__bytes_received = 0
        self.__bytes_read = 0
        self.__write_ptr = 0
        self.__read_ptr = 0
        self.__fullness = 0
        
        #reset status
        self.status_socket = SlimAudioBuffer.SOCKET_DISCONNECTED
        self.status_buffer = SlimAudioBuffer.BUFFER_EMPTY
        
    def __read_stream_http_header(self):
        """read stream header"""
        self.logger.debug('Reading stream header...')
        try:
            continu = True
            end_char = 0
            header = ''
            while continu:
                raw = self.socket.recv(1)
                if raw:
                    #unpack
                    char = struct.unpack('!1s', raw)
                    if len(char)==1:
                        char = char[0]
                    else:
                        #problem reading data
                        self.logger.error('Problem during stream header reading')
                        self.status_socket = SlimAudioBuffer.SOCKET_ERROR
                        self.disconnect()
                        break

                    #update number of end char found
                    if len(header)>=1 and (char=='\n' or char=='\r'):
                        end_char += 1
                    else:
                        end_char = 0
                        
                    #append char to header
                    header += char
                    
                    #end of header?
                    if end_char==4:
                        self.logger.debug('Stream header:%s' % (header.replace('\n','/n').replace('\r','/r')))
                        return header
                else:
                    #problem reading stream header
                    self.logger.error('Problem during stream header reading')
                    self.status_socket = SlimAudioBuffer.SOCKET_ERROR
                    self.disconnect()
                    break
        except socket.error, e:
            self.logger.error('Socket recv error')
            self.status_socket = SlimAudioBuffer.SOCKET_ERROR
            self.disconnect()
        
    def __buffering(self):
        """read data from socket"""
        try:
            buf = self.socket.recv(4096)
            if buf:
                #write buffer
                self.__write_buffer(buf)
            else:
                #nothing to read on socket, disconnect it
                self.disconnect()
        except socket.timeout, e:
            #timeout
            self.logger.error('Socket recv timeout')
            self.status_socket = SlimAudioBuffer.SOCKET_TIMEOUT
            self.disconnect()
        except socket.error, e:
            self.logger.error('Socket recv error')
            self.status_socket = SlimAudioBuffer.SOCKET_ERROR
            self.disconnect()
            
    def __write_buffer(self, buf):
        """write to circular buffer"""
        with self.buffer_lock:
            n = len(buf)
            #if n > self.__buffer_size - self.bytes_received:
            #    return False
            if n + self.__write_ptr > self.__buffer_size:
                #TODO stop writing buffer when read_ptr reached because with this way audio can be loosed
                #update buffer status
                self.logger.debug('Buffer is full')
                self.status_buffer = SlimAudioBuffer.BUFFER_FULL
                #data to write is greater than remaining space in buffer
                chunk = self.__buffer_size - self.__write_ptr
                #start to write data to end of buffer...
                self.__mv[self.__write_ptr:self.__write_ptr+chunk] = buf[0:chunk]
                #...and finish to write remaining data to buffer beginning
                self.__mv[0:n-chunk] = buf[chunk:n]
                #compute new write pointer position
                self.__write_ptr = n - chunk
            else:
                #free space in buffer can store data
                self.__mv[self.__write_ptr:self.__write_ptr+n] = buf[0:n]
                self.__write_ptr += n
            
            #reset pointer if end of buffer reached
            if self.__write_ptr == self.__buffer_size:
                self.__write_ptr = 0
            
            #save number of bytes received
            self.__bytes_received += n
            
            #update buffer fullness
            self.__fullness += n
            
    def read_buffer(self, size):
        """read data from circular buffer"""
        with self.buffer_lock:
            if self.__bytes_read + size > self.__bytes_received:
                if (self.__bytes_received - self.__bytes_read)==0:
                    #nothing left in buffer, return empty buffer
                    self.logger.debug('Nothing left in buffer, return None')
                    return None
                else:
                    #unable to return amount of requested data
                    #return only remaining data in buffer
                    self.logger.debug('Unable to return requested amount of data')
                    size = self.__bytes_received - self.__bytes_read
                
            if size + self.__read_ptr > self.__buffer_size:
                #end of buffer reached before reading requested amount of data
                chunk = self.__buffer_size - self.__read_ptr
                #read until end of buffer
                buf  = self.__mv[self.__read_ptr:self.__read_ptr+chunk].tobytes()
                #and read buffer beginning
                buf += self.__mv[0:size-chunk].tobytes()
                #compute new read pointer position
                self.__read_ptr = size - chunk
            else:
                #buffer contains enough data
                buf = self.__mv[self.__read_ptr:self.__read_ptr+size].tobytes()
                self.__read_ptr += size
            
            #reset read pointer if end of buffer reached
            if self.__read_ptr == self.__buffer_size:
                self.__read_ptr = 0;
            
            #update buffer fullness
            self.__fullness -= size
            
            #update number of read bytes
            self.__bytes_read += size
            
            #manage buffer status
            if self.__fullness<=self.__buffer_size*0.05:
                #buffer is low
                self.status_buffer = SlimAudioBuffer.BUFFER_LOW
            elif self.__fullness<=0:
                #buffer is empty
                self.__fullness = 0
                self.status_buffer = SlimAudioBuffer.BUFFER_EMPTY
            
            return buf
            
    def stop(self):
        """stop slim audio buffer"""
        self.running = False
        
        #reset
        self.reset(0)
        
    def run(self):
        """main process"""
        #init
        previous_buffer_status = self.status_buffer
        previous_socket_status = self.status_socket
        
        while self.running:
            if self.status_socket==SlimAudioBuffer.SOCKET_READ_HEADER:
                #socket ready to read header
                self.__http_header = self.__read_stream_http_header()
                self.status_socket = SlimAudioBuffer.SOCKET_CONNECTED
        
            elif self.status_socket==SlimAudioBuffer.SOCKET_CONNECTED:
                #buffer connected to stream
                #buffering data
                if self.status_buffer==SlimAudioBuffer.BUFFER_EMPTY:
                    #prebuffering data if buffer is empty
                    self.logger.debug('Prebuffering...')
                    for i in range(10):
                        self.__buffering()
                    #set buffer is ready
                    self.logger.debug('Prebuffering completed')
                    self.status_buffer = SlimAudioBuffer.BUFFER_READY
                else:
                    #buffering
                    #self.logger.debug('Buffering...')
                    self.__buffering()
            else:
                #buffer not connected to audio stream
                #nothing to do
                pass
                
            #status buffer callback
            if self.status_buffer_callback and previous_buffer_status!=self.status_buffer:
                self.logger.debug('Buffer status changed')
                self.status_buffer_callback(self.status_buffer)
                previous_buffer_status = self.status_buffer
                
            #status socket callback
            if self.status_socket_callback and previous_socket_status!=self.status_socket:
                self.logger.debug('Socket status changed')
                self.status_socket_callback(self.status_socket)
                previous_socket_status = self.status_socket
            
            #sleep only when not buffering
            if self.status_socket!=SlimAudioBuffer.SOCKET_CONNECTED:
                time.sleep(0.05)
            
    def get_fullness(self):
        """return buffer fullness"""
        return self.__fullness
        
    def get_size(self):
        """return buffer size"""
        return self.__buffer_size
    
    def get_bytes_received(self):
        """return amount of data received"""
        return self.__bytes_received
        
    def get_bytes_read(self):
        """return amount of data read"""
        return self.__bytes_read
        
    def get_http_header(self):
        """return http header"""
        return self.__http_header

        
        
        
        
class SlimAudio(threading.Thread):
    """SlimAudio manage audio stream playback"""
    PLAYER_INIT = 0
    PLAYER_PLAY = 1
    PLAYER_PAUSE = 2
    PLAYER_STOP = 3
    
    DECODER_NONE = 0
    DECODER_ERROR = 1
    DECODER_EMPTY = 2
    DECODER_NEED_DATA = 3
    DECODER_ENOUGH_DATA = 4
    DECODER_BUFFERING = 5
    
    def __init__(self, audio_format, status_player_callback, status_buffer_callback, status_decoder_callback):
        #init
        threading.Thread.__init__(self)
        self.running = True
        self.logger = logging.getLogger("SlimAudio")
        
        #members
        #audio format: 'p' = PCM, 'm' = MP3, 'f' = FLAC, 'w' = WMA, 'o' = Ogg., 'a' = AAC (& HE-AAC), 'l' = ALAC 
        self.audio_format = audio_format
        self.status_player_callback = status_player_callback
        self.status_buffer_callback = status_buffer_callback
        self.status_decoder_callback = status_decoder_callback
        self.status_player = SlimAudio.PLAYER_INIT
        self.status_decoder = SlimAudio.DECODER_NONE
        self.status_buffer = SlimAudioBuffer.BUFFER_EMPTY
        self.status_socket = SlimAudioBuffer.SOCKET_DISCONNECTED
        self.played_time_ms = 0
        self.play_start_time_ms = 0
        
        #objects
        self.gstreamer = None
        self.__init_gstreamer()
        self.slim_audio_buffer = SlimAudioBuffer(self.__status_buffer_callback, self.__status_socket_callback)
        
    def reset(self, buffer_size):
        """reset audio player"""
        #reset status
        self.status_player = SlimAudio.PLAYER_STOP
        self.status_decoder = SlimAudio.DECODER_NONE
        
        #save audio format
        
        
        #reset buffer
        self.slim_audio_buffer.reset(buffer_size)
        
        #reset player
        self.gstreamer.set_state(gst.STATE_READY)
        
    def stop(self):
        """Stop audio player"""
        self.running = False
        
        #stop player
        self.status_player = SlimAudio.PLAYER_STOP
        self.status_decoder = SlimAudio.DECODER_NONE
        self.gstreamer.set_state(gst.STATE_NULL)
        
        #stop buffer
        if self.slim_audio_buffer:
            self.slim_audio_buffer.stop()
            
    def run(self):
        """main process"""
        #init
        previous_player_status = self.status_player
        previous_decoder_status = self.status_decoder
        
        #start buffer
        self.slim_audio_buffer.start()
        
        while self.running:
            if self.status_buffer>=SlimAudioBuffer.BUFFER_READY:
                #buffer is ready
                while self.status_decoder==SlimAudio.DECODER_NEED_DATA:
                    #read data from buffer
                    buf = self.slim_audio_buffer.read_buffer(512)
                    #self.logger.debug('BufLen=%d  ReadBytes=%d RecvdBytes=%d' % (len(buf), self.slim_audio_buffer.get_bytes_read(), self.slim_audio_buffer.get_bytes_received()))
                    if buf==None:
                        #no buffer retrieved, check socket status
                        if self.status_socket==SlimAudioBuffer.SOCKET_DISCONNECTED:
                            #socket disconnected, it appears all stream was retrieved so nothing more to read
                            self.logger.debug('No more buffer')
                            self.status_decoder = SlimAudio.DECODER_EMPTY
                        else:
                            #socket not disconnected and no buffer retrieved, network seems to be very slow
                            self.logger.warning('Network is slow, buffering...')
                            self.status_decoder = SlimAudio.DECODER_BUFFERING
                    else:
                        #buffer contains data, inject it into gstreamer
                        self.gstreamer_appsrc.emit('push-buffer', gst.Buffer(buf))
                    
            #manage player status
            if previous_player_status!=self.status_player:
                if self.status_player_callback:
                    self.status_player_callback(self.status_player)
                previous_player_status = self.status_player
                
            #manage decoder status
            if previous_decoder_status!=self.status_decoder:
                if self.status_decoder_callback:
                    self.status_decoder_callback(self.status_decoder)
                previous_decoder_status = self.status_decoder
            
            #sleep
            time.sleep(0.1)
                    
    def __status_buffer_callback(self, status):
        """callback when buffer status change"""
        #just save buffer status
        #when buffer is ready, we can fill gstreamer buffer
        self.logger.debug('Buffer status changed [%s]' % str(status))
        self.status_buffer = status
    
    def __status_socket_callback(self, status):
        """callback when buffer socket status change"""
        self.logger.debug('Socket status changed [%s]' % str(status))
        
        #save socket status
        self.status_socket = status
        
        #callback
        if self.status_buffer_callback:
            self.status_buffer_callback(status)
        
        #manage player
        if status==SlimAudioBuffer.SOCKET_DISCONNECTED:
            #buffer socket disconnected, full stream was received, update gstreamer size. Mandatory to detect end of playback
            size = self.slim_audio_buffer.get_bytes_received()
            self.logger.debug('Buffer received %d bytes from stream' % size)
            self.gstreamer_appsrc.set_property('size', size)
            
    """def __init_gstreamer(self):
        #create pipeline
        self.gstreamer = gst.Pipeline('pipeline')
        
        #elements
        self.gstreamer_appsrc = gst.element_factory_make('appsrc', 'src')
        if self.audio_format=='m':
            #mpeg format (gst-plugins-ugly)
            self.gstreamer_decoder = gst.element_factory_make('mad', 'decoder')
        elif self.audio_format=='p':
            #pcm (gst-plugins-base)
            self.gstreamer_decoder = gst.element_factory_make('ffdec_adpcm_ms', 'decoder')
        elif self.audio_format=='f':
            #flac (gst-plugins-good)
            self.gstreamer_decoder = gst.element_factory_make('flacdec', 'decoder')
        elif self.audio_format=='w':
            #wma
            self.gstreamer_decoder = gst.element_factory_make('ffdec_wmav2', 'decoder')
        elif self.audio_format=='o':
            #ogg (gst-plugins-base)
            self.gstreamer_decoder = gst.element_factory_make('vorbisdec', 'decoder')
        elif self.audio_format=='a':
            #aac (gst-plugins-bad)
            self.gstreamer_decoder = gst.element_factory_make('faad', 'decoder')
            ffdec_aac
        elif self.audio_format=='p':
            #alac
            self.gstreamer_decoder = gst.element_factory_make('ffdec_alac', 'decoder')
        self.gstreamer_converter = gst.element_factory_make('audioconvert', 'converter')
        self.gstreamer_volume = gst.element_factory_make('volume', 'volume')
        self.gstreamer_sink = gst.element_factory_make('autoaudiosink', 'sink')
        
        #add elements to pipeline
        self.gstreamer.add(self.gstreamer_appsrc, self.gstreamer_decoder, self.gstreamer_converter, self.gstreamer_volume, self.gstreamer_sink)
        
        #link elements
        if self.audio_format=='m' or self.audio_format=='f' or self.audio_format=='o' or self.audio_format=='a':
            gst.element_link_many(self.gstreamer_appsrc, self.gstreamer_decoder, self.gstreamer_converter, self.gstreamer_volume, self.gstreamer_sink)
        else:
            gst.element_link_many(self.gstreamer_appsrc, self.gstreamer_decoder)
            gst.element_link_many(self.gstreamer_converter, self.gstreamer_volume, self.gstreamer_sink)
            self.gstreamer_decoder.connect("new-decoded-pad", self.__gstreamer_on_new_decoded_pad)
            
        #bus
        bus = self.gstreamer.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.__gstreamer_bus_message)
        
        #update gstreamer state
        if self.audio_format=='m' or self.audio_format=='f' or self.audio_format=='w' or self.audio_format=='o' or self.audio_format=='a' or self.audio_format=='p':
            self.gstreamer.set_state(gst.STATE_READY)
        else:
            #state updated in __gstreamer_on_new_decoded_pad callback
            pass
            
    def __gstreamer_on_new_decoded_pad(self, dbin, pad, islast):
        #gstreamer decodebin2 element link
        decoder = pad.get_parent()
        pipeline = decode.get_parent()
        converter = pipeline.get_by_name('converter')
        decoder.link(converter)
        self.gstreamer.set_state(gst.STATE_READY)"""
            
    def __init_gstreamer(self):
        """init gstreamer player"""
        """http://gstreamer.freedesktop.org/data/doc/gstreamer/head/gst-plugins-base-plugins/html/gst-plugins-base-plugins-appsrc.html"""
        #create pipeline
        self.gstreamer = gst.Pipeline('pipeline')
        
        #source
        self.gstreamer_appsrc = gst.element_factory_make('appsrc', 'src')
        self.gstreamer.add(self.gstreamer_appsrc)
        #decoder
        if self.audio_format=='m':
            #mpeg format (gst-plugins-ugly)
            self.logger.debug('Create mpeg decoder')
            self.gstreamer_decoder = gst.element_factory_make('mad', 'decoder')
        elif self.audio_format=='p':
            #pcm
            self.logger.debug('Create pcm decoder')
            self.gstreamer_decoder = gst.element_factory_make('ffdec_adpcm_ms', 'decoder')
        elif self.audio_format=='f':
            #flac (gst-plugins-good)
            self.logger.debug('Create flac decoder')
            self.gstreamer_decoder = gst.element_factory_make('flacdec', 'decoder')
        elif self.audio_format=='w':
            #wma
            self.logger.debug('Create wma decoder')
            self.gstreamer_decoder = gst.element_factory_make('ffdec_wmav2', 'decoder')
        elif self.audio_format=='o':
            #ogg (gst-plugins-base)
            self.logger.debug('Create vorbis decoder')
            self.gstreamer_decoder = gst.element_factory_make('vorbisdec', 'decoder')
        elif self.audio_format=='a':
            #aac (gst-plugins-bad)
            self.logger.debug('Create aac decoder')
            self.gstreamer_decoder = gst.element_factory_make('faad', 'decoder')
        elif self.audio_format=='p':
            #alac
            self.logger.debug('Create alac decoder')
            self.gstreamer_decoder = gst.element_factory_make('ffdec_alac', 'decoder')
        self.gstreamer.add(self.gstreamer_decoder)
        self.gstreamer_appsrc.link(self.gstreamer_decoder)
        self.gstreamer_appsrc.connect('enough-data', self.__gstreamer_enough_data)
        self.gstreamer_appsrc.connect('need-data', self.__gstreamer_need_data)
        #converter
        self.gstreamer_converter = gst.element_factory_make('audioconvert', 'converter')
        self.gstreamer.add(self.gstreamer_converter)
        self.gstreamer_decoder.link(self.gstreamer_converter)
        #volume
        self.gstreamer_volume = gst.element_factory_make('volume', 'volume')
        self.gstreamer.add(self.gstreamer_volume)
        self.gstreamer_converter.link(self.gstreamer_volume)
        #sink
        self.gstreamer_sink = gst.element_factory_make('autoaudiosink', 'sink')
        self.gstreamer.add(self.gstreamer_sink)
        self.gstreamer_volume.link(self.gstreamer_sink)
        #self.gstreamer_converter.link(self.gstreamer_sink)
        
        #bus
        bus = self.gstreamer.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.__gstreamer_bus_message)
        
        #player ready
        self.gstreamer.set_state(gst.STATE_READY)
        
    def __gstreamer_enough_data(self, *args):
        """gstreamer internal decoder has enough data"""
        #self.logger.debug('gstreamer: enough data')
        self.status_decoder = SlimAudio.DECODER_ENOUGH_DATA
        
    def __gstreamer_need_data(self, *args):
        """gstreamer internal decoder needs data"""
        #self.logger.debug('gstreamer: need data')
        self.status_decoder = SlimAudio.DECODER_NEED_DATA

    def __gstreamer_bus_message(self, bus, message):
        """catch gstreamer bus message"""
        """http://gstreamer.freedesktop.org/data/doc/gstreamer/head/gstreamer/html/gstreamer-GstMessage.html"""
        if message:
            if message.type==gst.MESSAGE_STATE_CHANGED:
                dummy, new_state, dummy = message.parse_state_changed()
                #self.logger.debug('gstreamer: state changed [%s]' % (str(new_state)))
                if new_state==gst.STATE_NULL:
                    #player created or stopped, reset playing time
                    self.played_time_ms = 0
                    self.play_start_time_ms = 0
                    self.status_player = SlimAudio.PLAYER_STOP
                elif new_state==gst.STATE_READY:
                    #player init
                    #if self.play_start_time_ms>0:
                    #    #save played time (after end of track)
                    #    self.played_time_ms += int(time.time() * 1000) - self.play_start_time_ms
                    #else:
                    #    #nothing played yet, reset member
                    #    self.played_time_ms = 0
                    self.play_start_time_ms = 0
                    self.status_player = SlimAudio.PLAYER_STOP
                elif new_state==gst.STATE_PAUSED:
                    if self.status_player==SlimAudio.PLAYER_PLAY:
                        #player paused, compute playing time between start time and now
                        current_time_ms = int(time.time() * 1000)
                        if current_time_ms>self.play_start_time_ms:
                            self.played_time_ms += current_time_ms - self.play_start_time_ms
                            self.play_start_time_ms = 0
                    self.status_player = SlimAudio.PLAYER_PAUSE
                elif new_state==gst.STATE_PLAYING:
                    #player start playing, save start time
                    self.play_start_time_ms = int(time.time() * 1000)
                    self.status_player = SlimAudio.PLAYER_PLAY
                    
            elif message.type==gst.MESSAGE_EOS:
                #update status
                self.logger.debug('gstreamer: end of playback')
                self.status_player = SlimAudio.PLAYER_STOP
                self.status_decoder = SlimAudio.DECODER_NONE
                #update played time
                self.played_time_ms += int(time.time() * 1000) - self.play_start_time_ms
                #set gstreamer is stopped
                self.gstreamer.set_state(gst.STATE_READY)
                
            elif message.type==gst.MESSAGE_ERROR:
                #error decoding stream
                self.logger.debug('gstreamer: error')
                self.logger.error(message.parse_error())
                #update status
                self.status_player = SlimAudio.PLAYER_STOP
                self.status_decoder = DECODER_ERROR
                #update played time
                self.played_time_ms += int(time.time() * 1000) - self.play_start_time_ms
                #set gstreamer is stopped
                self.gstreamer.set_state(gst.STATE_READY)
            elif message.type==gst.MESSAGE_WARNING:
                self.logger.debug('GSTREAMER: message_warning')
            elif message.type==gst.MESSAGE_INFO:
                self.logger.debug('GSTREAMER: message_info')
            elif message.type==gst.MESSAGE_TAG:
                self.logger.debug('GSTREAMER: message_tag')
            elif message.type==gst.MESSAGE_BUFFERING:
                self.logger.debug('GSTREAMER: message_buffering')
            elif message.type==gst.MESSAGE_STATE_DIRTY:
                self.logger.debug('GSTREAMER: message_state_dirty')
            elif message.type==gst.MESSAGE_STEP_DONE:
                self.logger.debug('GSTREAMER: message_step_done')
            elif message.type==gst.MESSAGE_CLOCK_PROVIDE:
                self.logger.debug('GSTREAMER: message_clock_provide')
            elif message.type==gst.MESSAGE_CLOCK_LOST:
                self.logger.debug('GSTREAMER: message_clock_lost')
            elif message.type==gst.MESSAGE_NEW_CLOCK:
                self.logger.debug('GSTREAMER: message_new_clock')
            elif message.type==gst.MESSAGE_STRUCTURE_CHANGE:
                self.logger.debug('GSTREAMER: message_structure_change')
            elif message.type==gst.MESSAGE_STREAM_STATUS:
                #self.logger.debug('GSTREAMER: message_stream_status')
                #self.logger.debug(message.parse_stream_status())
                pass
            elif message.type==gst.MESSAGE_APPLICATION:
                self.logger.debug('GSTREAMER: message_application')
            elif message.type==gst.MESSAGE_ELEMENT:
                self.logger.debug('GSTREAMER: message_element')
            elif message.type==gst.MESSAGE_SEGMENT_START:
                self.logger.debug('GSTREAMER: message_segment_start')
            elif message.type==gst.MESSAGE_SEGMENT_DONE:
                self.logger.debug('GSTREAMER: message_segment_done')
            elif message.type==gst.MESSAGE_LATENCY:
                self.logger.debug('GSTREAMER: message_latency')
            elif message.type==gst.MESSAGE_ASYNC_START:
                self.logger.debug('GSTREAMER: message_async_start')
            elif message.type==gst.MESSAGE_ASYNC_DONE:
                self.logger.debug('GSTREAMER: message_async_done')
            elif message.type==gst.MESSAGE_REQUEST_STATE:
                self.logger.debug('GSTREAMER: message_request_state')
            elif message.type==gst.MESSAGE_STEP_START:
                self.logger.debug('GSTREAMER: message_step_start')
            elif message.type==gst.MESSAGE_QOS:
                self.logger.debug('GSTREAMER: message_qos')
            elif message.type==gst.MESSAGE_PROGRESS:
                self.logger.debug('GSTREAMER: message_progress')
            elif message.type==gst.MESSAGE_ANY:
                self.logger.debug('GSTREAMER: message_any')
                
    def set_replay_gain(self, replay_gain):
        """set reaply gain"""
        self.gstreamer_volume.set_property('volume', replay_gain / 65536.0 * 10.0)
        
    def mute(self):
        """mute player"""
        self.gstreamer_volume.set_property('volume', 0.0)
                    
    def start_playback(self, server_ip, server_port, http_header):
        """start playback"""
        self.logger.debug('Start playback')
        #connect buffer
        if self.slim_audio_buffer.connect(server_ip, server_port, http_header):
            #buffer connected, launch gstreamer
            self.gstreamer.set_state(gst.STATE_PLAYING)
        else:
            #buffer failed to connect
            self.logger.error('Unable to connect audio buffer, playback canceled')
        
    def stop_playback(self):
        """stop playback"""
        self.logger.debug('Stop playback')
        self.gstreamer.set_state(gst.STATE_PAUSED)
        
    def pause_playback(self, duration=0):
        """pause playback, launch unpause timer if necessary"""
        self.logger.debug('Pause playback (duration=%d)' % duration)
        #pause gstreamer
        self.gstreamer.set_state(gst.STATE_PAUSED)
        #launch unpause timer
        if duration>0:
            gobject.timeout_add(duration, self.unpause_playback)
        
    def unpause_playback(self):
        """unpause playback"""
        self.logger.debug('Unpause playback')
        self.gstreamer.set_state(gst.STATE_PLAYING)
        return False #useful for unpause timer
        
    def get_decoder_fullness(self):
        """gstreamer fullness"""
        #simulate decoder buffer fullness according to decoder and buffer status
        if self.status_decoder==SlimAudio.DECODER_NEED_DATA:
            #buffer isn't full
            fullness = self.gstreamer_appsrc.get_property('max-bytes') * 0.75
        elif self.status_decoder==SlimAudio.DECODER_ENOUGH_DATA:
            #buffer is full
            fullness = self.gstreamer_appsrc.get_property('max-bytes')
        else:
            #decoder not ready
            fullness = 0
        return fullness
        
    def get_decoder_buffer_size(self):
        """gstreamer buffer size"""
        return self.gstreamer_appsrc.get_property('max-bytes')
        
    def __get_hr_playing_time(self, time):
        """convert in HR specified time (in seconds)"""
        temp = time
        hours = 0
        minutes = 0
        #hours
        if temp>=3600:
            hours = int(time / 3600)
            temp = time - hours * 3600
        #minutes
        if temp>=60:
            minutes = int(temp / 60)
            temp = temp - minutes * 60
        #seconds
        seconds = temp
        if hours>0:
            return '%02d:%02d:%02d' % (hours,minutes,seconds)
        else:
            return '%02d:%02d' % (minutes,seconds)
        
    def get_decoder_playing_time(self):
        """return playing time (in ms)"""
        if self.play_start_time_ms!=0:
            #music is playing
            played_time_ms = 0
            if self.status_player==SlimAudio.PLAYER_PLAY:
                #compute playing time according to current time
                played_time_ms = self.played_time_ms + int(time.time() * 1000) - self.play_start_time_ms
            elif self.status_player==SlimAudio.PLAYER_PAUSE:
                #nothing to compute, just return played_time_ms
                played_time_ms = self.played_time_ms
            elif self.status_player==SlimAudio.PLAYER_STOP:
                #player is not playing but send current playing time 
                played_time_ms = self.played_time_ms
            #self.logger.debug('%d - %d - %d' % (self.played_time_ms, int(time.time()*1000), self.play_start_time_ms))
            #self.logger.debug('played_time_ms = %s' % self.__get_hr_played_time(played_time_ms))
            return played_time_ms
        else:
            #music is not playing
            return self.played_time_ms
            
    def get_buffer_size(self):
        """return SlimAudioBuffer size"""
        return self.slim_audio_buffer.get_size()
        
    def get_buffer_fullness(self):
        """return SlimAudioBuffer fullness"""
        fullness = self.slim_audio_buffer.get_fullness()
        #adjust fullness
        if fullness<0:
            fullness = 0
        return fullness
        
    def get_buffer_bytes_received(self):
        """return amount of bytes received by SlimAudioBuffer"""
        return self.slim_audio_buffer.get_bytes_received()
        
    def get_buffer_http_header(self):
        """return stream http header read after buffer connection"""
        return self.slim_audio_buffer.get_http_header()