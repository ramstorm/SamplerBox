#
#  SamplerBox
#
#  author:    Joseph Ernest (twitter: @JosephErnest, mail: contact@samplerbox.org)
#  url:       http://www.samplerbox.org/
#  license:   Creative Commons ShareAlike 3.0 (http://creativecommons.org/licenses/by-sa/3.0/)
#
#  samplerbox.py: Main file
#


#########################################
# CONFIG
#########################################

AUDIO_DEVICE_ID1 = 0
AUDIO_DEVICE_ID2 = 2
SAMPLES_DIR = "."                       # The root directory containing the sample-sets. Example: "/media/" to look for samples on a USB stick / SD card
USE_SERIALPORT_MIDI = False             # Set to True to enable MIDI IN via SerialPort (e.g. RaspberryPi's GPIO UART pins)
USE_I2C_7SEGMENTDISPLAY = False         # Set to True to use a 7-segment display via I2C
USE_BUTTONS = False                     # Set to True to use momentary buttons (connected to RaspberryPi's GPIO pins) to change preset
MAX_POLYPHONY = 2                       # This can be set higher, but 80 is a safe value
LOCAL_CONFIG = 'local_config.py'	# Local config filename
DEBUG = False                           # Enable to switch verbose logging on

# Load local config if available
import os.path
if os.path.isfile(LOCAL_CONFIG):
    execfile(LOCAL_CONFIG)

#########################################
# IMPORT
# MODULES
#########################################

import wave
import time
import numpy
import os
import sys
import re
import pyaudio
import threading
from chunk import Chunk
import struct
import rtmidi_python as rtmidi
import samplerbox_audio                         # legacy audio (pre RPi-2 models)
#import samplerbox_audio_neon as samplerbox_audio # ARM NEON instruction set

CARDS = sys.argv[1].split(',')
CARD1 = CARDS[0]
CARD2 = CARDS[1] if len(CARDS) == 2 else ''
MIDI_CH = int(sys.argv[4])
MAX_POLYPHONY = int(sys.argv[3])
SAMPLES_DIR = sys.argv[2] + '/samples'
KICKS_DIR = sys.argv[2] + '/kicks'

#########################################
# SLIGHT MODIFICATION OF PYTHON'S WAVE MODULE
# TO READ CUE MARKERS & LOOP MARKERS
#########################################

class waveread(wave.Wave_read):

    def initfp(self, file):
        self._convert = None
        self._soundpos = 0
        self._cue = []
        self._loops = []
        self._ieee = False
        self._file = Chunk(file, bigendian=0)
        if self._file.getname() != 'RIFF':
            raise Error, 'file does not start with RIFF id'
        if self._file.read(4) != 'WAVE':
            raise Error, 'not a WAVE file'
        self._fmt_chunk_read = 0
        self._data_chunk = None
        while 1:
            self._data_seek_needed = 1
            try:
                chunk = Chunk(self._file, bigendian=0)
            except EOFError:
                break
            chunkname = chunk.getname()
            if chunkname == 'fmt ':
                self._read_fmt_chunk(chunk)
                self._fmt_chunk_read = 1
            elif chunkname == 'data':
                if not self._fmt_chunk_read:
                    raise Error, 'data chunk before fmt chunk'
                self._data_chunk = chunk
                self._nframes = chunk.chunksize // self._framesize
                self._data_seek_needed = 0
            elif chunkname == 'cue ':
                numcue = struct.unpack('<i', chunk.read(4))[0]
                for i in range(numcue):
                    id, position, datachunkid, chunkstart, blockstart, sampleoffset = struct.unpack('<iiiiii', chunk.read(24))
                    self._cue.append(sampleoffset)
            elif chunkname == 'smpl':
                manuf, prod, sampleperiod, midiunitynote, midipitchfraction, smptefmt, smpteoffs, numsampleloops, samplerdata = struct.unpack(
                    '<iiiiiiiii', chunk.read(36))
                for i in range(numsampleloops):
                    cuepointid, type, start, end, fraction, playcount = struct.unpack('<iiiiii', chunk.read(24))
                    self._loops.append([start, end])
            chunk.skip()
        if not self._fmt_chunk_read or not self._data_chunk:
            raise Error, 'fmt chunk and/or data chunk missing'

    def getmarkers(self):
        return self._cue

    def getloops(self):
        return self._loops


#########################################
# MIXER CLASSES
#
#########################################

class PlayingSound:

    def __init__(self, sound, note, velocity, doublenote, iskick):
        self.sound = sound
        self.pos = 0
        self.fadeoutpos = 0
        self.isfadeout = False
        self.note = note
        self.velocity = velocity
        self.doublenote = doublenote
        self.iskick = iskick

    def fadeout(self, i):
        self.isfadeout = True

    def stop(self):
        try:
            if CARD2 and self.iskick:
                playingsounds2.remove(self)
            else:
                playingsounds1.remove(self)
        except:
            pass


class Sound:

    def __init__(self, filename, midinote, velocity, samplegain, doublenote):
        wf = waveread(filename)
        self.fname = filename
        self.midinote = midinote
        self.velocity = velocity
        self.samplegain = samplegain
        self.doublenote = doublenote
        if wf.getloops():
            self.loop = wf.getloops()[0][0]
            self.nframes = wf.getloops()[0][1] + 2
        else:
            self.loop = -1
            self.nframes = wf.getnframes()

        self.data = self.frames2array(wf.readframes(self.nframes), wf.getsampwidth(), wf.getnchannels())

        wf.close()

    def play(self, note, velocity):
        global kicknote
        global kickbend
        actual_velocity = (1-globalvelocitysensitivity + (globalvelocitysensitivity * (velocity/127.0)))*self.samplegain
        iskick = self.midinote == kicknote
        bend = int(kickbend * ((84.0 - note) / 127)) if (iskick or self.doublenote == kicknote) else 0
        snd = PlayingSound(self, note + bend, actual_velocity, self.doublenote, iskick)
        if CARD2 and iskick:
            playingsounds2.append(snd)
        else:
            playingsounds1.append(snd)
        return snd

    def frames2array(self, data, sampwidth, numchan):
        if sampwidth == 2:
            npdata = numpy.fromstring(data, dtype=numpy.int16)
        elif sampwidth == 3:
            npdata = samplerbox_audio.binary24_to_int16(data, len(data)/3)
        if numchan == 1:
            npdata = numpy.repeat(npdata, 2)
        return npdata

FADEOUTLENGTH = 30000
FADEOUT = numpy.linspace(1., 0., FADEOUTLENGTH)            # by default, float64
FADEOUT = numpy.power(FADEOUT, 6)
FADEOUT = numpy.append(FADEOUT, numpy.zeros(FADEOUTLENGTH, numpy.float32)).astype(numpy.float32)
SPEED = numpy.power(2, numpy.arange(0.0, 84.0)/12).astype(numpy.float32)

samples = {}
playingnotes = {}
sustainplayingnotes = []
sustain = False
playingsounds1 = []
playingsounds2 = []
globalvolume = 10 ** (-12.0/20)  # -12dB default global volume
globaltranspose = 0
kicknote = 2
kickbend = 0


#########################################
# AUDIO AND MIDI CALLBACKS
#
#########################################

def AudioCallback1(in_data, frame_count, time_info, status):
    global playingsounds1
    rmlist = []
    playingsounds1 = playingsounds1[-MAX_POLYPHONY:]
    b = samplerbox_audio.mixaudiobuffers(playingsounds1, rmlist, frame_count, FADEOUT, FADEOUTLENGTH, SPEED, globalvolume)
    for e in rmlist:
        try:
            playingsounds1.remove(e)
        except:
            pass
#    odata = (b.astype(numpy.int16)).tostring()
    odata = b.tostring()
    return (odata, pyaudio.paContinue)

def AudioCallback2(in_data, frame_count, time_info, status):
    global playingsounds2
    rmlist = []
    playingsounds2 = playingsounds2[-MAX_POLYPHONY:]
    b = samplerbox_audio.mixaudiobuffers(playingsounds2, rmlist, frame_count, FADEOUT, FADEOUTLENGTH, SPEED, globalvolume)
    for e in rmlist:
        try:
            playingsounds2.remove(e)
        except:
            pass
#    odata = (b.astype(numpy.int16)).tostring()
    odata = b.tostring()
    return (odata, pyaudio.paContinue)

def MidiCallback(message, time_stamp):
    global playingnotes, sustain, sustainplayingnotes
    global preset
    global kickpreset
    global kickbend
    messagetype = message[0] >> 4
    if messagetype == 15:    # Ignore system messages
        return

    messagechannel = (message[0] & 15) + 1
    if messagechannel != MIDI_CH:    # Only listen to channel supplied in argv
        return

    note = message[1] if len(message) > 1 else None
    midinote = note
    velocity = message[2] if len(message) > 2 else None

    if messagetype == 9: # Note on
        if note == 1:    # Use note 1 as note off
            for s in playingsounds2:
                s.fadeout(50)
            for s in playingsounds1:
                s.fadeout(50)
        else:
            midinote += globaltranspose
            try:
                samples[midinote, velocity].play(midinote, velocity)
                doublenote = samples[midinote, velocity].doublenote
                if doublenote != 0:
                    samples[doublenote, velocity].play(doublenote, velocity)
            except:
                pass

    elif (messagetype == 11) and (note == 2):  # CC #2
        kickbend = velocity

    elif messagetype == 14:  # Pitch bend
        kickbend_tmp = (note | (velocity << 7)) - 8192
        if kickbend_tmp >= 0:
            kickbend = int(kickbend_tmp * (127 / 8191.0))

    elif messagetype == 12:  # Program change
        if note < 64:
            kickpreset = note
        else:
            preset = note - 64
        LoadSamples()

    elif (messagetype == 11) and (note == 64) and (velocity < 64):  # sustain pedal off
        for n in sustainplayingnotes:
            n.fadeout(50)
        sustainplayingnotes = []
        sustain = False

    elif (messagetype == 11) and (note == 64) and (velocity >= 64):  # sustain pedal on
        sustain = True


#########################################
# LOAD SAMPLES
#
#########################################

LoadingThread = None
LoadingInterrupt = False


def LoadSamples():
    global LoadingThread
    global LoadingInterrupt

    if LoadingThread:
        LoadingInterrupt = True
        LoadingThread.join()
        LoadingThread = None

    LoadingInterrupt = False
    LoadingThread = threading.Thread(target=ActuallyLoad)
    LoadingThread.daemon = True
    LoadingThread.start()

NOTES = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]

def ActuallyLoad():
    global preset
    global samples
    global playingsounds1
    global globalvolume, globaltranspose
    global globalvelocitysensitivity
    playingsounds1 = []
    samples = {}
    globalvolume = 10 ** (-12.0/20)  # -12dB default global volume
    globaltranspose = 0
    globalvelocitysensitivity = 0 # default midi velocity sensitivity 

    ActuallyLoadKick()

    basename = next((f for f in os.listdir(SAMPLES_DIR) if f.startswith("%d " % preset)), None)      # or next(glob.iglob("blah*"), None)
    if basename:
        dirname = os.path.join(SAMPLES_DIR, basename)
    if not basename:
        print 'Preset empty: %s' % preset
        display("E%03d" % preset)
        return
    print 'Preset loading: %s (%s)' % (preset, basename)
    display("L%03d" % preset)

    definitionfname = os.path.join(dirname, "definition.txt")
    if os.path.isfile(definitionfname):
        with open(definitionfname, 'r') as definitionfile:
            for i, pattern in enumerate(definitionfile):
                try:
                    if r'%%volume' in pattern:        # %%paramaters are global parameters
                        globalvolume *= 10 ** (float(pattern.split('=')[1].strip()) / 20)
                        continue
                    if r'%%transpose' in pattern:
                        globaltranspose = int(pattern.split('=')[1].strip())
                        continue
                    if r'%%velocitysensitivity' in pattern:
                        globalvelocitysensitivity = float(pattern.split('=')[1].strip())
                        continue
                    defaultparams = {'midinote': '0', 'velocity': '127', 'samplegain': '100', 'doublenote': '0', 'notename': ''}
                    if len(pattern.split(',')) > 1:
                        defaultparams.update(dict([item.split('=') for item in pattern.split(',', 1)[1].replace(' ', '').replace('%', '').split(',')]))
                    pattern = pattern.split(',')[0]
                    pattern = re.escape(pattern.strip())
                    pattern = pattern.replace(r"\%midinote", r"(?P<midinote>\d+)").replace(r"\%velocity", r"(?P<velocity>\d+)").replace(r"\%samplegain", r"(?P<samplegain>\d+)")\
                                     .replace(r"\%doublenote", r"(?P<doublenote>\d+)").replace(r"\%notename", r"(?P<notename>[A-Ga-g]#?[0-9])").replace(r"\*", r".*?").strip()    # .*? => non greedy
                    for fname in os.listdir(dirname):
                        if LoadingInterrupt:
                            return
                        m = re.match(pattern, fname)
                        if m:
                            info = m.groupdict()
                            midinote = int(info.get('midinote', defaultparams['midinote']))
                            velocity = int(info.get('velocity', defaultparams['velocity']))
                            samplegain = float(info.get('samplegain', defaultparams['samplegain']))/100
                            doublenote = int(info.get('doublenote', defaultparams['doublenote']))
                            notename = info.get('notename', defaultparams['notename'])
                            if notename:
                                midinote = NOTES.index(notename[:-1].lower()) + (int(notename[-1])+2) * 12
                            samples[midinote, velocity] = Sound(os.path.join(dirname, fname), midinote, velocity, samplegain, doublenote)
                except:
                    print "Error in definition file, skipping line %s." % (i+1)

    else:
        for midinote in range(0, 127):
            if LoadingInterrupt:
                return
            file = os.path.join(dirname, "%d.wav" % midinote)
            if os.path.isfile(file):
                samples[midinote, 127] = Sound(file, midinote, 127, 100, 0)

    initial_keys = set(samples.keys())
    for midinote in xrange(128):
        lastvelocity = None
        for velocity in xrange(128):
            if (midinote, velocity) not in initial_keys:
                samples[midinote, velocity] = lastvelocity
            else:
                if not lastvelocity:
                    for v in xrange(velocity):
                        samples[midinote, v] = samples[midinote, velocity]
                lastvelocity = samples[midinote, velocity]
        if not lastvelocity:
            for velocity in xrange(128):
                try:
                    samples[midinote, velocity] = samples[midinote-1, velocity]
                except:
                    pass
    if len(initial_keys) > 0:
        print 'Preset loaded: ' + str(preset)
        display("%04d" % preset)
    else:
        print 'Preset empty: ' + str(preset)
        display("E%03d" % preset)


def ActuallyLoadKick():
    global kickpreset
    global kicknote
    global samples
    global playingsounds2
    global globalvolume, globaltranspose
    global globalvelocitysensitivity

    if CARD2:
        playingsounds2 = []
    dirname = KICKS_DIR
    if not dirname:
        print 'Kick dir missing'
        return
    print 'Kick preset loading: %s (%s)' % (kickpreset, dirname)
    display("L%03d" % kickpreset)

    definitionfname = os.path.join(dirname, "definition.txt")
    if os.path.isfile(definitionfname):
        with open(definitionfname, 'r') as definitionfile:
            for i, pattern in enumerate(definitionfile):
                try:
                    if r'%%volume' in pattern:        # %%paramaters are global parameters
                        globalvolume *= 10 ** (float(pattern.split('=')[1].strip()) / 20)
                        continue
                    if r'%%transpose' in pattern:
                        globaltranspose = int(pattern.split('=')[1].strip())
                        continue
                    if r'%%velocitysensitivity' in pattern:
                        globalvelocitysensitivity = float(pattern.split('=')[1].strip())
                        continue
                    defaultparams = {'midinote': '0', 'velocity': '127', 'samplegain': '100', 'doublenote': '0', 'notename': ''}
                    if len(pattern.split(',')) > 1:
                        defaultparams.update(dict([item.split('=') for item in pattern.split(',', 1)[1].replace(' ', '').replace('%', '').split(',')]))
                    pattern = pattern.split(',')[0]
                    pattern = re.escape(pattern.strip())
                    pattern = pattern.replace(r"\%midinote", r"(?P<midinote>\d+)").replace(r"\%velocity", r"(?P<velocity>\d+)").replace(r"\%samplegain", r"(?P<samplegain>\d+)")\
                                     .replace(r"\%doublenote", r"(?P<doublenote>\d+)").replace(r"\%notename", r"(?P<notename>[A-Ga-g]#?[0-9])").replace(r"\*", r".*?").strip()    # .*? => non greedy
                    for fname in os.listdir(dirname):
                        if LoadingInterrupt:
                            return
                        m = re.match(pattern, fname)
                        if m:
                            info = m.groupdict()
                            midinote = int(info.get('midinote', defaultparams['midinote']))
                            velocity = int(info.get('velocity', defaultparams['velocity']))
                            samplegain = float(info.get('samplegain', defaultparams['samplegain']))/100
                            doublenote = 0   # not used for kicks
                            notename = info.get('notename', defaultparams['notename'])
                            if notename:
                                midinote = NOTES.index(notename[:-1].lower()) + (int(notename[-1])+2) * 12
                            if midinote != kickpreset:
                                continue

                            samples[kicknote, velocity] = Sound(os.path.join(dirname, fname), kicknote, velocity, samplegain, doublenote)
                            print 'Kick preset loaded: ' + str(kickpreset)
                            display("%04d" % kickpreset)
                            return
                except:
                    print "Error in kick definition file, skipping line %s." % (i+1)

    else:
        if LoadingInterrupt:
            return
        file = os.path.join(dirname, "%d.wav" % kicknote)
        if os.path.isfile(file):
            samples[kicknote, 127] = Sound(file, kicknote, 127, 100, 0)
            print 'Kick preset loaded: ' + str(kickpreset)
            display("%04d" % kickpreset)
            return

    print 'Kick preset empty: ' + str(kickpreset)
    display("E%03d" % kickpreset)


#########################################
# OPEN AUDIO DEVICE
#
#########################################

p = pyaudio.PyAudio()
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    # Find card name using aplay -l
    # print "checking device: " + str(i) + " " + str(dev['maxOutputChannels']) + " " + sys.argv[1] + " " + dev['name']
    if dev['maxOutputChannels'] > 0:
        if CARD1 in dev['name']:
            print "CARD1 device: index=" + str(i) + " name=" + dev['name']
            AUDIO_DEVICE_ID1 = i
        elif CARD2 and CARD2 in dev['name']:
            print "CARD2 device: index=" + str(i) + " name=" + dev['name']
            AUDIO_DEVICE_ID2 = i

try:
    stream1 = p.open(format=pyaudio.paInt16, channels=2, rate=44100, frames_per_buffer=128, output=True,
                    input=False, output_device_index=AUDIO_DEVICE_ID1, stream_callback=AudioCallback1)
    print 'Opened audio1: ' + p.get_device_info_by_index(AUDIO_DEVICE_ID1)['name']
    if CARD2:
        stream2 = p.open(format=pyaudio.paInt16, channels=2, rate=44100, frames_per_buffer=128, output=True,
                        input=False, output_device_index=AUDIO_DEVICE_ID2, stream_callback=AudioCallback2)
        print 'Opened audio2: ' + p.get_device_info_by_index(AUDIO_DEVICE_ID2)['name']
except:
    print "Invalid Audio Device ID: " + str(AUDIO_DEVICE_ID1) + " or " + str(AUDIO_DEVICE_ID2)
    print "Here is a list of audio devices:"
    for i in range(p.get_device_count()):
        dev = p.get_device_info_by_index(i)
        # Remove input device (not really useful on a Raspberry Pi)
        if dev['maxOutputChannels'] > 0:
            print str(i) + " -- " + dev['name']
    exit(1)


#########################################
# BUTTONS THREAD (RASPBERRY PI GPIO)
#
#########################################

if USE_BUTTONS:
    import RPi.GPIO as GPIO

    lastbuttontime = 0

    def Buttons():
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(18, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        global preset, lastbuttontime
        while True:
            now = time.time()
            if not GPIO.input(18) and (now - lastbuttontime) > 0.2:
                lastbuttontime = now
                preset -= 1
                if preset < 0:
                    preset = 127
                LoadSamples()

            elif not GPIO.input(17) and (now - lastbuttontime) > 0.2:
                lastbuttontime = now
                preset += 1
                if preset > 127:
                    preset = 0
                LoadSamples()

            time.sleep(0.020)

    ButtonsThread = threading.Thread(target=Buttons)
    ButtonsThread.daemon = True
    ButtonsThread.start()


#########################################
# 7-SEGMENT DISPLAY
#
#########################################

if USE_I2C_7SEGMENTDISPLAY:
    import smbus

    bus = smbus.SMBus(1)     # using I2C

    def display(s):
        for k in '\x76\x79\x00' + s:     # position cursor at 0
            try:
                bus.write_byte(0x71, ord(k))
            except:
                try:
                    bus.write_byte(0x71, ord(k))
                except:
                    pass
            time.sleep(0.002)

    display('----')
    time.sleep(0.5)

else:

    def display(s):
        pass


#########################################
# MIDI IN via SERIAL PORT
#
#########################################

if USE_SERIALPORT_MIDI:
    import serial

    ser = serial.Serial('/dev/ttyAMA0', baudrate=38400)       # see hack in /boot/cmline.txt : 38400 is 31250 baud for MIDI!

    def MidiSerialCallback():
        message = [0, 0, 0]
        while True:
            i = 0
            while i < 3:
                data = ord(ser.read(1))  # read a byte
                if data >> 7 != 0:
                    i = 0      # status byte!   this is the beginning of a midi message: http://www.midi.org/techspecs/midimessages.php
                message[i] = data
                i += 1
                if i == 2 and message[0] >> 4 == 12:  # program change: don't wait for a third byte: it has only 2 bytes
                    message[2] = 0
                    i = 3
            MidiCallback(message, None)

    MidiThread = threading.Thread(target=MidiSerialCallback)
    MidiThread.daemon = True
    MidiThread.start()


#########################################
# LOAD FIRST SOUNDBANK
#
#########################################

#preset = 0
preset = int(sys.argv[5])
kickpreset = 0
LoadSamples()


#########################################
# MIDI DEVICES DETECTION
# MAIN LOOP
#########################################

midi_in = rtmidi.MidiIn()
for port in midi_in.ports:
    if 'Midi Through' in port:
        midi_in.callback = MidiCallback
        midi_in.open_port(port)
        print 'Opened MIDI: ' + port
        break
while True:
    time.sleep(100)

