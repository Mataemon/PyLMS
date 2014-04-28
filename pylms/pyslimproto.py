import socket
import struct
import sys
import logging
import threading
import time
from collections import namedtuple
import Queue
from slimaudio import SlimAudio, SlimAudioBuffer


class SlimProtoHeartbeat(threading.Thread):
    """Heartbeat to say server player is connected"""
    def __init__(self, heartbeat_callback):
        threading.Thread.__init__(self)
        self.running = True
        self.logger = logging.getLogger("SlimProtoHeartbeat")
        self.heartbeat_callback = heartbeat_callback
        self.__interval = 4
        
    def set_interval(self, interval):
        """set heartbeat interval"""
        self.logger.debug('interval changed to "%d"' % interval)
        self.__interval = interval
        
    def stop(self):
        """stop heartbeat"""
        self.heartbeat_callback = None
        self.running = False
        
    def run(self):
        """process"""
        #just send heartbeat at regular interval
        self.logger.debug('SlimProtoHeartbeat started')
        while self.running:
            if self.heartbeat_callback:
                self.heartbeat_callback()
            time.sleep(self.__interval)
        self.logger.debug('SlimProtoHeartbeat ended')


            
            
            
            
            
class SlimProtoSocketCommand():
    def __init__(self, event, command):
        self.event = event
        self.command = command
            
            
class SlimProtoSocket(threading.Thread):
    """Low level socket message manager. Only queue received message and send queued messages. No need to use thread lock using this way"""
    STATUS_DISCONNECTED = 0
    STATUS_CONNECTED = 1
    STATUS_ERROR = 2

    def __init__(self, server_ip, server_port=3483, status_socket_callback=None):
        """Constructor"""
        threading.Thread.__init__(self)
        self.running = True
        self.logger = logging.getLogger("SlimProtoSocket")
        
        #parameters
        self.__status_socket_callback = status_socket_callback
        self.socket = None
        self.server_ip = server_ip
        self.server_port = server_port
        
        #members
        self.status = SlimProtoSocket.STATUS_DISCONNECTED
        self.send_queue = Queue.Queue()
        self.recv_queue = Queue.Queue()
        
    def connect(self):
        """connect to server"""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.logger.debug('Connecting to %s:%d...' % (self.server_ip, self.server_port))
            self.socket.connect((self.server_ip, self.server_port))
            self.logger.debug('Connected!')
            self.status = SlimProtoSocket.STATUS_CONNECTED
        except socket.error, e:
            self.logger.critical('Connection failed: %s' % e)
            self.status = SlimProtoSocket.STATUS_ERROR
            return False
        self.socket.settimeout(0.5)
        return True
        
    def disconnect(self):
        """disconnect socket"""
        self.logger.debug('Disconnected from server')
        self.status = SlimProtoSocket.STATUS_DISCONNECTED
        if self.socket:
            self.socket.close()
    
    def __recv_command(self):
        """Receive message from socket"""
        try:
            data = self.socket.recv(2)
        except socket.timeout:
            return None, None
            
        if not data:
            # Connection closed on server?
            self.logger.critical('Connection closed by server?')
            self.disconnect()
            return None, None

        try:
            length = struct.unpack('!H', data)[0]
            data = self.socket.recv(length)
            cmd_header, cmd_data = struct.unpack('!4s%is' % (length-4), data)
            return cmd_header, cmd_data
        except:
            #error occured
            return None, None
        
    def __send_command(self, command):
        """send message to socket"""
        sent = self.socket.send(command)
        #self.logger.debug('Sent bytes to socket = %d' % sent)
        
    def put_command(self, event, command):
        """put command to send queue"""
        #self.logger.debug('Put command "%s"' % event)
        self.send_queue.put(SlimProtoSocketCommand(event,command))
        
    def get_command(self):
        """get command from received queue"""
        return self.recv_queue.get()
    
    def command_available(self):
        """is command available in queue?"""
        return not self.recv_queue.empty()
        
    def stop(self):
        """stop slimprotosocket"""
        #stop process
        self.running = False
        
        #disconnect socket
        self.disconnect()
        
        #clear queue messages
        while not self.recv_queue.empty():
            self.recv_queue.get()
        while not self.send_queue.empty():
            self.send_queue.get()
        
    def run(self):
        """process"""
        previous_status = self.status
        while self.running:
            if self.status==SlimProtoSocket.STATUS_CONNECTED:
                #receive commands
                header, data = self.__recv_command()
                if header:
                    self.recv_queue.put((header, data))
                    
                #send commands
                if not self.send_queue.empty():
                    cmd = self.send_queue.get()
                    #self.logger.debug('Send command "%s"' % cmd.event)
                    self.__send_command(cmd.command)
            
            #manage status
            if previous_status!=self.status:
                if self.__status_socket_callback:
                    self.__status_socket_callback(self.status)
                previous_status = self.status
            
            #sleep
            time.sleep(0.05)

            
            
            
            
            
            
            
            

class SlimProto(threading.Thread):
    """SlimProto manage message between server and client"""
    STATUS_INIT = 0
    STATUS_CONNECTED = 1
    STATUS_HELO = 2
    STATUS_READY = 3
    
    DSCO_OK = 0
    DSCO_LOCAL = 1
    DSCO_DISCONNECT = 2
    DSCO_UNREACHABLE = 3
    DSCO_TIMEOUT = 4
    
    def __init__(self, server_ip, server_port=3483, mac_address="00:00:00:00:00:03"):
        threading.Thread.__init__(self)
        self.running = True
        self.logger = logging.getLogger("SlimProto")
        
        #parameters
        self.server_ip = server_ip
        self.server_port = server_port
        self.mac_address = self.__hex_to_byte(mac_address)
        
        #members
        self.status = SlimProto.STATUS_INIT
        self.time_for_jiffies = int(time.time() * 1000)
        # The Device ID of the player. '2' is squeezebox. '3' is softsqueeze, '4' is squeezebox2,
        # '5' is transporter, '6' is softsqueeze3, '7' is receiver, '8' is squeezeslave, 
        # '9' is controller, '10' is boom, '11' is softboom, '12' is squeezeplay 
        # Tested with squeezeplay
        self.device_id = 8
        #FIXME revision=255
        self.revision = 255
        self.capabilities = 'model=squeezeslave,modelName=SlimTang,mp3,MaxSampleRate=192000'
        self.__http_header = ''
        self.__status_player = SlimAudio.PLAYER_INIT
        self.__status_player_previous = SlimAudio.PLAYER_INIT
        
        #objects
        self.slim_proto_socket = None
        self.slim_audio = None
        
    def __reset(self, audio_format='m'):
        """reset everything (called after each end of track)"""
        self.logger.debug('Reseting...')
        
        #reset members
        self.status = SlimProto.STATUS_INIT
        
        #stop existing slim audio
        if self.slim_audio:
            self.slim_audio.stop()
            #force object deletion
            del self.slim_audio
            self.slim_audio = None
                
        #launch audio player
        self.slim_audio = SlimAudio(audio_format, self.__status_player_callback, self.__status_buffer_callback, self.__status_decoder_callback)
        self.slim_audio.start()
            
        #update status
        self.status = SlimProto.STATUS_READY
        
    def stop(self):
        """stop slimproto"""
        #stop main thread
        self.running = False
        
        #send BYE
        self.send_BYE()
        
        #stop slim proto socket
        if self.slim_proto_socket:
            self.slim_proto_socket.stop()
            
        #stop slim audio
        if self.slim_audio:
            self.slim_audio.stop()
        
    def run(self):
        """process"""
        #launch slim proto socket
        self.slim_proto_socket = SlimProtoSocket(self.server_ip, self.server_port, self.__status_socket_callback)
        if not self.slim_proto_socket.connect():
            #unable to connect, stop right now
            self.stop()
        else:
            self.status = SlimProto.STATUS_CONNECTED
            self.slim_proto_socket.start()
        
        #introduce player sending helo
        self.send_HELO()
        self.status==SlimProto.STATUS_HELO

        #reset slim audio and some status
        self.__reset()
        
        #then answer to server requests
        heartbeat_last = 0
        heartbeat_send = False
        unpause_jiffies = 0
        while self.running:
            #init
            heartbeat_send = False
            strm = None
        
            if self.status==SlimProto.STATUS_READY:
                if self.slim_proto_socket.command_available():
                    #get command
                    header, data = self.slim_proto_socket.get_command()
                    
                    if header=='strm':
                        #strm command
                        strm = self.__parse_strm(data[:24])
                        if len(self.__http_header)==0 and len(data)>24:
                            self.__http_header = self.__get_http_header(data[18:])
                        
                        if strm['command']=='s':
                            #start command
                            self.logger.debug('From server: strm-s')
                            #send stream connection flushed
                            self.send_STAT_STMf()
                            #reset slimproto (set audio format)
                            self.__reset(strm['formatbyte'])
                            #reset slim audio and adjust buffer size (15 secondes)
                            #self.logger.debug('SampleSize=%s SampleRate=%s Channels=%s BufferSize=%d' % (strm['pcmsamplesize'], strm['pcmsamplerate'], strm['pcmchannels'], buffer_size))
                            buffer_size = self.__convert_sample_size(strm['pcmsamplesize']) * self.__convert_sample_rate(strm['pcmsamplerate']) * self.__convert_sample_channels(strm['pcmchannels']) * 15
                            self.slim_audio.reset(buffer_size)
                            #start playback
                            self.slim_audio.start_playback(self.__http_header['server_ip'], self.__http_header['server_port'], self.__http_header['http_header'])
                            #TODO manage autostart. if autostart==0 send STMl
                            #send connected
                            self.send_STAT_STMc()
                            #TODO manage buffer threshold
                        elif strm['command']=='p':
                            #pause command
                            self.logger.debug('From server: strm-p')
                            #get pause interval in replay_gain field
                            duration = 0
                            try:
                                duration = int(strm['replay_gain'])
                            except ValueError,e:
                                duration = 0
                            #pause playback
                            self.slim_audio.pause_playback(duration)
                        elif strm['command']=='u':
                            #unpause command
                            self.logger.debug('From server: strm-u')
                            #get unpause jiffies in replay_gain field (to sync players)
                            unpause_jiffies = 0
                            try:
                                unpause_jiffies = int(strm['replay_gain'])
                            except ValueError,e:
                                unpause_jiffies = 0
                            #unpause playback
                            if unpause_jiffies==0:
                                self.slim_audio.unpause_playback()
                            else:
                                #unpause done in run() when unpause_jiffies reached
                                pass
                        elif strm['command']=='q':
                            #stop command
                            self.logger.debug('From server: strm-q')
                            self.slim_audio.reset(0)
                        elif strm['command']=='t':
                            #status command
                            #self.logger.debug('From server: strm-t')
                            #self.logger.debug('--->strm-t response<---')
                            heartbeat_last = int(time.time()*1000)
                            self.send_STAT_STMt(strm['replay_gain'])
                        elif strm['command']=='f':
                            #flush command
                            self.logger.debug('From server: strm-f')
                            self.slim_audio.reset(0)
                        elif strm['command']=='a':
                            #skip-ahead command
                            self.logger.debug('From server: strm-a')
                            
                    if header=='audg':
                        self.logger.debug('Command audg')
                        audg = self.__parse_audg(data[:18])
                        self.logger.debug('Audio gainL=%d gainR=%d' % (audg['gainL'], audg['gainR']))
                        #adjust audio gain
                        self.slim_audio.set_replay_gain(audg['gainL'])
                        
                    #TODO manage other commands
            
            #unpause_jiffies
            if unpause_jiffies>0:
                #unpause when unpause_jiffies reached
                if self.__jiffies()>=unpause_jiffies:
                    self.slim_audio.unpause_playback()
                    unpause_jiffies = 0
                
            #hearbeat
            if self.__status_player==SlimAudio.PLAYER_PLAY:
                if (int(time.time()*1000) - heartbeat_last)>1000:
                    heartbeat_last = int(time.time()*1000)
                    #self.logger.debug('--->heartbeat<---')
                    self.send_STAT_STMt(0)
            
            #sleep
            time.sleep(0.05)
            
    def __convert_sample_size(self, sample_size):
        """convert received sample size to real one"""
        if sample_size=='0':
            return 8
        elif sample_size=='1':
            return 16
        elif sample_size=='2':
            return 20
        elif sample_size=='3':
            return 32
        elif sample_size=='?':
            return 16
    
    def __convert_sample_rate(self, sample_size):
        """convert received sample rate to real one"""
        if sample_size=='0':
            return 11000
        elif sample_size=='1':
            return 22000
        elif sample_size=='2':
            return 32000
        elif sample_size=='3':
            return 44100
        elif sample_size=='4':
            return 48000
        elif sample_size=='5':
            return 8000
        elif sample_size=='6':
            return 12000
        elif sample_size=='7':
            return 16000
        elif sample_size=='8':
            return 24000
        elif sample_size=='9':
            return 96000
        elif sample_size=='?':
            return 44100
            
    def __convert_sample_channels(self, sample_size):
        """convert received sample channels to real one"""
        if sample_size=='1':
            return 1
        elif sample_size=='2':
            return 2
        elif sample_size=='?':
            return 2
        
    def __hex_to_byte(self, hexStr):
        """
        Convert a string hex byte values into a byte string. The Hex Byte values may
        or may not be colon separated.
        http://code.activestate.com/recipes/510399-byte-to-hex-and-hex-to-byte-string-conversion/
        """
        bytes = []
        hexStr = ''.join( hexStr.split(":") )
        for i in range(0, len(hexStr), 2):
            bytes.append( chr( int (hexStr[i:i+2], 16 ) ) )
        return bytes
        
    def __jiffies(self):
        # uint32 should last almost 50 days
        return int(time.time() * 1000) - self.time_for_jiffies
        
    def __parse_strm(self, cmd_data):
        """parse strm command"""
        keys = ['command', 'autostart', 'formatbyte', 'pcmsamplesize', 'pcmsamplerate', 'pcmchannels', 'pcmendian', 'threshold',
                'spdif_enable', 'trans_period', 'trans_type', 'flags', 'output_threshold', 'reserved', 'replay_gain', 
                'server_port', 'server_ip']
        values = struct.unpack('!7c7BIHI', cmd_data)
        return dict(zip(keys, values))
        
    def __parse_audg(self, cmd_data):
        """parse audg command"""
        keys = ['old_gainL', 'old_gainR', 'fixed_digital', 'preamp', 'gainL', 'gainR']
        values = struct.unpack('!llBBll', cmd_data)
        return dict(zip(keys, values))
        
    def __get_http_header(self, cmd_data):
        """get http header from strm data"""
        (server_port, server_ip, http_header) = struct.unpack('!HI%is' % (len(cmd_data)-6), cmd_data)
        self.logger.debug("HTTP header %s" % http_header)
        if not server_ip:
            server_ip = self.server_ip
        return {'server_port': server_port,
                 'server_ip':   server_ip,
                 'http_header': http_header}
                 
    def __status_player_callback(self, status):
        """event when player status change"""
        self.logger.debug('Player status change [%d]' % status)
        if status==SlimAudio.PLAYER_STOP:
            #end of stream or user stop playback
            #normal end of playback
            self.send_STAT_STMu()
        elif status==SlimAudio.PLAYER_PLAY:
            if self.__status_player_previous==SlimAudio.PLAYER_PAUSE:
                #unpause confirmation
                self.send_STAT_STMr()
            else:
                #playback of new track started
                self.send_STAT_STMs()
        elif status==SlimAudio.PLAYER_PAUSE:
            #pause confirmation
            self.send_STAT_STMp()
        #save status
        self.__status_player_previous = self.__status_player
        self.__status_player = status
        
    def __status_buffer_callback(self, status):
        """event when buffer status change"""
        self.logger.debug('Buffer status change [%d]' % status)
        if status==SlimAudioBuffer.SOCKET_DISCONNECTED:
            #stream disconnected, all stream data has been buffered
            self.send_DSCO(SlimProto.DSCO_OK)
        elif status==SlimAudioBuffer.SOCKET_CONNECTED:
            #just connected, send http header
            self.send_RESP(self.slim_audio.get_buffer_http_header())
        elif status==SlimAudioBuffer.SOCKET_TIMEOUT:
            #stream timeout
            self.send_DSCO(SlimProto.DSCO_TIMEOUT)
        elif status==SlimAudioBuffer.SOCKET_ERROR:
            #stream timeout
            self.send_DSCO(SlimProto.DSCO_LOCAL)
            
    def __status_decoder_callback(self, status):
        """event when decoder status change"""
        self.logger.debug('Decoder status changed [%d]' % status)
        if status==SlimAudio.DECODER_EMPTY:
            #decoder has no data to decode
            self.send_STAT_STMd()
        elif status==SlimAudio.DECODER_ERROR:
            #error decoding stream
            self.send_STAT_STMn()
        elif status==SlimAudio.DECODER_BUFFERING:
            #decoder is buffering
            self.send_STAT_STMo()
            
    def __status_socket_callback(self, status):
        """event when proto socket status change"""
        self.logger.debug('Socket proto status changed [%d]' % status)
        #if status==SlimProtoSocket.STATUS_DISCONNECTED:
        #    self.stop()
        #elif status==SlimProtoSocket.STATUS_ERROR:
        #    self.stop()
        
    def __push_command(self, event, command):
        """centralize command sending"""
        if self.slim_proto_socket:
            self.slim_proto_socket.put_command(event, command)
            
    def send_HELO(self):
        """send HELO to sever"""
        length = 36 + len(self.capabilities)
        self.__push_command('HELO', struct.pack('!4sIBB6c28x%is' % len(self.capabilities),
            'HELO',
            length,
            self.device_id,
            self.revision,
            self.mac_address[0],
            self.mac_address[1], 
            self.mac_address[2], 
            self.mac_address[3], 
            self.mac_address[4], 
            self.mac_address[5], 
            self.capabilities))
        self.logger.debug('To server : HELO : device_id=%d revision=%d mac=%s capabilities=%s' % (self.device_id, self.revision, self.mac_address, self.capabilities))
            
    def send_BYE(self):
        """send BYE! to server"""
        self.__push_command('BYE!', struct.pack('!4sIB', 'BYE!', 1, 0))
        self.logger.debug('To server : BYE!')
            
    def send_RESP(self, header):
        """send RESP to server"""
        self.__push_command('RESP', struct.pack('!4sI%is' % (len(header)),
            'RESP',
            8 + len(header) - 8,
            header))
        self.logger.debug('To server : RESP : header=%s' % (header.replace('\n','/n').replace('\r','/r')))
        
    def send_DSCO(self, disconnect_code):
        """send DSCO to server"""
        self.__push_command('DSCO', struct.pack('!4sIB',
            'DSCO',
            9 - 8,
            disconnect_code))
        self.logger.debug('To server : DSCO : disconnect_code=%s' % (disconnect_code))
        
    def send_SETD(self, name):
        """send SETD (name) to server"""
        self.__push_command('SETD', struct.pack('!4sIB%is' % (len(name)), 
            'SETD',     #command
            9 + len(name) - 8,     #length
            0,          #param 0 is player name
            name        #name
            ))
        self.logger.debug('To server : SETD : name=%s' % (name))
        
    def __send_STAT(self, event_code, timestamp):
        """send STAT to server"""
        if self.status==SlimProto.STATUS_READY:
            #process is running
            buffer_size = self.slim_audio.get_buffer_size()
            buffer_fullness = self.slim_audio.get_buffer_fullness()
            bytes_received = self.slim_audio.get_buffer_bytes_received()
            decoder_buffer_size = self.slim_audio.get_decoder_buffer_size()
            decoder_buffer_fullness = self.slim_audio.get_decoder_fullness()
            elapsed_milliseconds = self.slim_audio.get_decoder_playing_time()
            elapsed_seconds = int(elapsed_milliseconds / 1000)
            server_timestamp = timestamp
            jiffies = self.__jiffies()
        else:
            #nothing running (no track to play?), send default values
            buffer_size = 0
            buffer_fullness = 0
            bytes_received = 0
            decoder_buffer_size = 0
            decoder_buffer_fullness = 0
            elapsed_seconds = 0
            elapsed_milliseconds = 0
            server_timestamp = timestamp
            jiffies = self.__jiffies()
    
        if event_code!='STMt':
            self.logger.debug('To server : STAT: event_code=%s, buffer_size=%i, buffer_fullness=%i, bytes_received=%i, jiffies=%i, decoder_buffer_size=%i, decoder_buffer_fullness=%i, elapsed_seconds=%i, elapsed_milliseconds=%i, server_timestamp=%i' % (event_code, buffer_size, buffer_fullness, bytes_received, jiffies, decoder_buffer_size, decoder_buffer_fullness, elapsed_seconds, elapsed_milliseconds, server_timestamp))
            
        """cmd = struct.pack('!4s', 'STAT')
        cmd += struct.pack('!I', 53)
        cmd += struct.pack('!4s', event_code)
        cmd += struct.pack('!B', 0)
        cmd += struct.pack('!B', 0)
        cmd += struct.pack('!B', 0)
        cmd += struct.pack('!I', buffer_size)
        cmd += struct.pack('!I', buffer_fullness)
        cmd += struct.pack('!Q', bytes_received)
        cmd += struct.pack('!H', 65535)
        cmd += struct.pack('!I', jiffies)
        cmd += struct.pack('!I', decoder_buffer_size)
        cmd += struct.pack('!I', decoder_buffer_fullness)
        cmd += struct.pack('!I', elapsed_seconds)
        cmd += struct.pack('!H', 0)
        cmd += struct.pack('!I', elapsed_milliseconds)
        cmd += struct.pack('!I', server_timestamp)
        cmd += struct.pack('!H', 0)
        self.__push_command('STAT-%s' % event_code, cmd)"""
        
        self.__push_command('STAT-%s' % event_code, struct.pack('!4sI4s3BIIQH4IHIIH', 
            'STAT', #(32b)
            53, #
            event_code, #event code (32b)
            0, #number of consecutive CRLF (8b)
            0, #MAS initialized (8b)
            0, #MAS mode (8b)
            buffer_size, #buffer size (32b)
            buffer_fullness, #buffer fullness (32b)
            bytes_received, #bytes received (64b)
            65534, #signal strength (16b)
            jiffies, #jiffies (32b)
            decoder_buffer_size, #decoder buffer size (32b)
            decoder_buffer_fullness, #decoder buffer fullness (32b)
            elapsed_seconds, #elapsed seconds of current stream (32b)
            0, #voltage (16b)
            elapsed_milliseconds, #elapsed millisecond of current stream (32b)
            server_timestamp, #server timestamp (32b)
            0)) # error code (16b)
            
    def send_STAT_STMa(self):
        #Autostart
        #Track started. Probably Squeezebox v1 only. 
        self.__send_STAT('STMa', 0)
        
    def send_STAT_STMc(self):
        #Connect
        #srtm-s command received. Guaranteed to be the first response to an strm-s. 
        self.__send_STAT('STMc', 0)
    
    def send_STAT_STMd(self):
        #Decoder ready
        #Instruct server that we are ready for the next track (if any). Sometimes (unfortunately) this will also indicate an error.
        self.__send_STAT('STMd', 0)
    
    def send_STAT_STMe(self):
        #Stream connection Established 
        self.__send_STAT('STMe', 0)
    
    def send_STAT_STMf(self):
        #Connection flushed 
        #Streaming track flushed (in response to strm-f) or playback stopped (in response to strm-q). The number of STMf responses which may be received in various circumstances is not well defined
        self.__send_STAT('STMf', 0)
    
    def send_STAT_STMh(self):
        #HTTP headers received 
        #from the streaming connection.
        self.__send_STAT('STMh', 0)
    
    def send_STAT_STMl(self):
        #Buffer threshold reached
        #When strm-s autostart=0/2.
        self.__send_STAT('STMl', 0)
    
    def send_STAT_STMn(self):
        #Not Supported
        #Decoder does not support file format or a decoding error has occurred. May include format-specific error code.
        self.__send_STAT('STMn', 0)
    
    def send_STAT_STMo(self):
        #Output Underrun
        #No more decoded (uncompressed) data to play; triggers rebuffering.
        self.__send_STAT('STMo', 0)
    
    def send_STAT_STMp(self):
        #Pause
        #Confirmation of Pause.
        self.__send_STAT('STMp', 0)

    def send_STAT_STMr(self):
        #Resume
        #Confirmation of resume. (Not used during playout)
        self.__send_STAT('STMr', 0)
    
    def send_STAT_STMs(self):
        #Track Started
        #Playback of a new track has started.
        self.__send_STAT('STMs', 0)
    
    def send_STAT_STMt(self, timestamp):
        #Timer
        #A simple hearbeat, can be periodic or a response to strm-t
        self.__send_STAT('STMt', timestamp)

    def send_STAT_STMu(self):
        #Underrun
        #Normal end of playback. 
        self.__send_STAT('STMu', 0)
        
