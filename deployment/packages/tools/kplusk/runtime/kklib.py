"""
Give access to native K+K library on Windows and Linux Systems

Depending on System Platform the following libraries are requested:
    Windows:
        64 bit: KK_Library_64.dll
        32 bit: KK_FX80E.dll
    Raspberry Pi OS (ARM):
        64 bit: libkk_linux_aarch64_cdecl.so
        32 bit: libkk_linux_arm32_cdecl.so
    Linux:
        64 bit: libkk_library_64_cdecl.so
        32 bit: libkk_library_32_cdecl.so

@author: Loryn Brendes, lb@brendes.de

History

version date        description
1.0     2019-02-20  created
1.0.1   2020-03-24  correction: platform.architecture() 
                    explicit c_int32
                    KK-Library min version 18.1.5
        2020-03-25  windows only
1.0.2   2020-04-08  KK-Library min version 18.1.6
1.0.3   2020-10-12  Linux support  
                    KK-Library min version 18.1.10
                    report_TCP_log: NSZ log types added
                    open_TCP_log: NSZ modes added
                    renamed: set_decimal_separator
                    added:
                        KK_ERR_NOT_SUPPORTED
                        get_firmware_version
                        has_FRAM
                        set_NSZ_calibration_data
                        remote_login 
1.0.4   2021-02-08  KK-Library min version 18.2.4
                    open_TCP_log: PHASEPREDECESSORLOG, USERLOG1, USERLOG2 added
1.1.0   2021-05-28  KK-Library min version 19.0.2
                    added:
                        KK_Err_Reconnected
                        open_TCP_log_time
                    changed:
                        stop_save_report_data without parameter dbg_id 
1.2     2021-11-18  KK-Library min version 19.1.2
                    added:
                        is_serial_device
                        get_FHR_settings, set_FHR_settings
                        helper classes FHRData, FHRSettings
1.3     2022-01-05  KK-Library min version 19.2.0
                    added:
                        open_TCP_log_type
                        send_TCP_data
1.4     2022-10-09  KK-Library min version 19.3.0
                    send_TCP_data delivers response
1.5     2023-01-04  KK-Library min version 19.3.1
                    added:
                        get_device_start_state
                        set_send_7016
1.6     2024-04-24  KK-Library min version 20.2
                    Raspberry Pi OS (ARM) added
                    catch exception setlocale
                    set_debug_log with param error_only
                    set_debug_stream added
                    get_lib_name added
1.6.1   2024-05-03  error code KK_DLL_EXCEPTION added
                    KK-Library min version 20.2.1
1.7	    2024-12-04  KK-Library min version 20.2.6
                    added:
                        request_connected_user
                        reset_device
2.0     2025-05-07  KK-Library min version 21.0.0
                    added: 
                        KK_Report, Phase
                        get infos from FXE report header (header_to_*)
                        get_kk_report
2.1     2025-06-03  KK-Library min version 21.1.0
                    added optional parameter format_fxe in open_TCP_log*
"""
from builtins import str

__all__ = ['NativeLib', 'NativeLibError', 'ErrorCode', 'KK_Result', 'DebugLogType', 'FHRData', 'FHRSettings', \
           'KK_ReportType', 'KK_Report', 'Phase']
__version__ = '2.1'

import ctypes
from ctypes import c_bool, c_byte, c_char, c_char_p, c_double, c_int32, c_int64, \
    c_uint, c_uint16, c_uint32, \
    byref, Structure
from enum import Enum
import locale
import math
import platform
import struct
import sys
from typing import Self


#-------------------------------------------------------------------------
# helper
#-------------------------------------------------------------------------

def bytesToHex(value: bytearray) -> str:
    s = "".join("%02X" % b for b in value)
    return s

def bytearray2string(barray: bytearray) -> str:
    # string in bytearray is terminated by 0
    if barray[0] == 0:
        return None
    else:
        i = barray.index(0)
        hilf = barray[:i]
        return hilf.decode()

def phase_decimal_places_from_frequency(aFreq: c_double) -> int:
    """The accuracy of a phase measurement depends on the measured frequency.
    The higher the frequency, the fewer decimal places.
    @param aFreq: frequency in hz
    @return: number of decimal places
    """
    aFreq = abs(aFreq)
    # 32.768 MHz
    if aFreq > 32768000.0:
        Result = 7
    # 4.096 MHz
    elif aFreq > 4096000.0:
        Result = 8
    # 512 kHz
    elif aFreq > 512000.0:
        Result = 9
    # 32 kHz
    elif aFreq > 32000.0:
         Result = 10
    else:
      Result = 11
    return Result


#-------------------------------------------------------------------------
# Infos from report header of FXE measurement
#
# 16 bit report header: mmmmiiiippss0000
#  m: 4 Bit Mode
#  i: 4 Bit Intervall
#  p: 2 Bit PPI
#  s: 2 Bit Scrambler
#  0: 4 Bit 0
#
#-------------------------------------------------------------------------

class ReportMode(Enum):
    """Report modes of FXE measurements"""
    RM_PHASE = 0
    RM_PHASE_AVERAGE = 1
    RM_FREQUENCY = 2
    RM_FREQUENCY_AVERAGE = 3
    RM_PHASE_DIFFERENCE = 4
    RM_PHASE_AVERAGE_DIFFERENCE = 5

class ScramblerMode(Enum):
    SCR_OFF = 0
    SCR_AUTO = 1
    SCR_TRIM = 2
    SCR_IN_USE = 3
    """Scrambler is controlled by another user"""

class PPIMode(Enum):
    PPI_DEFAULT = 0
    PPI_USER_CONTROLLED = 1
    # 2 = invalid
    PPI_IN_USE = 3
    """PPI is controlled by device itself or by another user"""


_ms = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]

def header_to_interval_ms(aHeader: int) -> int:
    """Delivers report intervall in ms or 0, if not a FXE measurement"""
    if (aHeader < 0) or (aHeader >= 0x7000):
        Result = 0
    else:
        aHeader = (aHeader & 0x0F00) >> 8
        if aHeader > 0xD:
            Result = 0
        else:
            Result = _ms[aHeader]
    return Result

def header_to_PPI_mode(aHeader: int) -> int:
    """Delivers PPI mode of FXE measurement
    0, 1, 3 = PPI_DEFAULT .. PPI_IN_USE
    -1 = not a FXE measurement or invalid PPI mode
    """
    if (aHeader < 0) or (aHeader >= 0x7000):
        Result = -1
    else:
        aHeader = (aHeader & 0x00C0) >> 6
        if aHeader == 2:
            Result = -1
        else:
            # 0..3 -> default .. in_use
            Result = aHeader
    return Result

def header_to_report_mode(aHeader: int) -> int:
    """Delivers report mode of FXE measurement
    0 .. 5 = RM_PHASE .. RM_PHASE_AVERAGE_DIFFERENCE
    -1 = not a FXE measurement
    """
    if (aHeader < 0) or (aHeader >= 0x7000):
        Result = -1
    else:
        aHeader = (aHeader & 0xF000) >> 12
        if aHeader > 5:
            Result = -1
        else:
            # 0..5 -> Phase..Phase Average Difference
            Result = aHeader
    return Result

def header_to_scrambler_mode(aHeader: int) -> int:
    """Delivers scrambler mode of FXE measurement
    0 .. 3 = SCR_OFF .. SCR_IN_USE
    -1 = not a FXE measurement
    """
    if (aHeader < 0) or (aHeader >= 0x7000):
        Result = -1
    else:
        aHeader = (aHeader & 0x0030) >> 4
        # 0..3 -> Off .. in_use
        Result = aHeader
    return Result


#-------------------------------------------------------------------------
# Library exception class
#-------------------------------------------------------------------------

class NativeLibError(Exception):
    """Exception class for Exceptions thrown by class NativeLib."""
    pass


#-------------------------------------------------------------------------
# Library return class
#-------------------------------------------------------------------------

# Enumerate error codes
class ErrorCode(Enum):
    """Enumeration of error codes, used in KK_Result.result_code"""
    KK_NO_ERR = 0
    """successful operation"""
    
    KK_ERR = 1
    """operation failed"""
    
    KK_ERR_ENUM_SERIAL = 2
    """Enumeration of serial ports failed"""
   
    KK_ERR_ENUM_USB = 3
    """Enumeration of USB devices failed"""

    KK_ERR_ENUM_SERIAL_USB = 4
    """Enumeration of serial ports and USB devices failed"""
        
    KK_ERR_BUFFER_TOO_SMALL = 5
    """String in KK_Result.data truncated to 1024 character"""
        
    KK_ERR_BUFFER_OVERFLOW = 6
    """Lost of data occurred. App does not read fast enough"""

    KK_ERR_WRITE = 7
    """Writing via current connection failed"""
        
    KK_ERR_SERVER_DOWN = 8
    """Connection to K+K server has broken, server is down"""
        
    KK_ERR_DEVICE_NOT_CONNECTED = 9
    """Connection to K+K device is interrupted (no usb cable)"""
        
    KK_HARDWARE_FAULT = 10
    """K+K device does not send measurement data. Measurement hardware
    should be checked.
    Connection to K+K device is closed.
    KK_Result.data contains error message
    """
        
    KK_PARAM_ERROR = 11
    """Parameter in function call has invalid value"""
        
    KK_CMD_IGNORED = 12
    """Command rejected (no connection, waiting queue full)"""
    
    KK_ERR_NOT_SUPPORTED = 13
    """Called function is not supported by K+K device"""
    
    KK_ERR_RECONNECTED = 14
    """Reconnection of an interrupted connection"""
    
    KK_DLL_EXCEPTION = 15
    """Exception error, please report to K+K Messtechnik"""

    KK_DEVICE_RESETTED = 16
    """Device resetted after reset command"""

        
        
class KK_Result:
    """Result class returned by most NativeLib methods"""
    result_code: ErrorCode
    """Operation result code (value of ErrorCode)"""
    data: str
    """Operation string result or error message, 
    if result_Code != KK_NO_ERR
    """
    int_value: int
    """Operation int value, set by some methods
    (e.g. start_TCP_server)
    """
        
    def __init__(self):
        """Initialize no error results"""
        self.result_code = ErrorCode.KK_NO_ERR
        self.data = None
        self.int_value = 0


#-------------------------------------------------------------------------
# Error strings from native library
#-------------------------------------------------------------------------

_ERR_SOURCE_NOT_FOUND = "Source-ID "


#-------------------------------------------------------------------------
# Maximal channel count 
#-------------------------------------------------------------------------

KK_MAX_CHANNELS = 24


#-------------------------------------------------------------------------
# DebugLogType
#-------------------------------------------------------------------------

class DebugLogType(Enum):
    """Defines debug log types for method NativeLib.set_debug_log_limit
    Specifies file management of debug output files.
    """
    LOG_UNLIMITED = 0
    """All debug outputs are written to the same file, reopen overwrites 
    existing debug file.
    File size is unlimited. Default value
    """

    LOG_OVERWRITE = 1
    """All debug outputs are written to the same file, reopen overwrites 
    existing debug file.
    File size is limited. If size is reached old debug outputs will 
    be overwritten.
    """

    LOG_CREATE_NEW = 2
    """A new debug file is created, filename is appended by date and 
    time stamp.
    File size is limited. If size is reached a new file is created.
    """


#-------------------------------------------------------------------------
# FHR settings: helper classes for get_FHR_seetings, set_FHR_sttings
#-------------------------------------------------------------------------

class FHRData:
    """Defines FHR setting for one channel
    """
    NominalFreq: str
    LOFreq: str
    enabled: bool
    
    def __init__(self, s: str = ""):
        if not self.from_string(s):
            raise NativeLibError( \
                "Invalid string representation of FHRData: "+s)
        
    def clear(self):
        self.NominalFreq = '0'
        self.LOFreq = '0'
        self.enabled = False

    def to_string(self) -> str:
        """Returns string representation of FHRData
        """
        if self.enabled:
            s = '1'
        else:
            s = '0'
        return self.NominalFreq+';'+self.LOFreq+';'+s        

    def from_string(self, s: str = "") -> bool:
        """Converts string representation to FHRData
        @param s: string representation of FHRData or empty string
        @return: True, if s is a valid string representation
        """
        self.clear()
        if s == "":
            return True
        else:
            l_ = s.split(';')
            if len(l_) < 3:
                return False
            self.NominalFreq = l_[0]
            self.LOFreq = l_[1]
            if l_[2] == '0':
                self.enabled = False
            else:
                self.enabled = True
            return True

 
class FHRSettings:
    """Defines FHR settings for KK_MAX_CHANNELS
    """
    FHRChannels = [FHRData() for i in range(1, KK_MAX_CHANNELS)]
    
    def __init__(self, s: str = ""):
        if not self.from_string(s):
            raise NativeLibError( \
                "Invalid string representation of FHRSettings: "+s)

    def clear(self):
        for fhr in self.FHRChannels:
            fhr.clear()
    
    def to_string(self) -> str:
        """Returns string representation of FHRSettings
        """
        s = self.FHRChannels[0].to_string()
        for i in range(2, KK_MAX_CHANNELS):
            s = s+'/'+self.FHRChannels[i-1].to_string()
        return s
    
    def from_string(self, s: str) -> bool:
        """Converts string representation to FHRSettings
        @param s: string representation of FHRSettings or empty string
        @return: True, if s is a valid string representation
        """
        self.clear()
        aBool = True

        if s == "":
            return aBool
        else:
            # split into channel strings                
            l_ = s.split('/')
            limit = len(l_)
            if limit > KK_MAX_CHANNELS:
                limit = KK_MAX_CHANNELS
            
            for i in range(1, limit):
                if not self.FHRChannels[i-1].from_string():
                    aBool = False
                
            return aBool


#-------------------------------------------------------------------------
# KK own representation of Phase values
#-------------------------------------------------------------------------

class Phase:
    """The concept of a phase means a floating point number that is always 
    increasing. To limit the loss of precision, it is stored internally in 
    two parts: the integer part High and the fractional part Low
    """
    High: int    # Integer part, unit = 1 Period
    Low: float   # 0 <= Low < 1, max 35 bit, unit = 1 Period

    def __init__(self, aHigh: int, aLow: float):
        self.High = aHigh
        self.Low = aLow

    @classmethod
    def from_float(cls, aFloat: float) -> Self:
        aHigh = math.trunc(aFloat)
        aLow = aFloat - aHigh
        return cls(aHigh, aLow)

    def normalize(self):
        """Shifts integer part of Low to High"""
        high = self.High
        low = self.Low

        if (high + low) < 0:
            if low > 0:  # no '>=' here!
                high = high + (math.trunc(low) + 1)
                # makes low negative: -1 < Low <= 0
                low = low - (math.trunc(low) + 1)
            else:
                high = high + math.trunc(low)
                low = low - math.trunc(low)
        else:
            if low < 0:
                high = high + (math.trunc(low) - 1)
                low = low - (math.trunc(low) - 1)
            else:
                high = high + math.trunc(low)
                low = low - math.trunc(low)

        self.High = high
        self.Low = low

    def to_float(self) -> float:
        """Delivers float with loss of precision"""
        return self.High + self.Low
    
    def to_str26(self, decimals: int) -> str:
        """ Formats self to floating point string with <decimal> decimal
        places and locale decimal separator. The resul string has a length
        of 26 characters
        @param decimals: number of decimal places, must be in range (7 .. 11)
        @return: 26 character string representaion
        """
        # decimals must be in range 7 .. 11
        if decimals < 7:
            decimals = 7
        elif decimals > 11:
            decimals = 11

        self.normalize()

        # format fractional part
        sFormat = "%."+str(decimals)+"f"
        low_str = locale.format_string(sFormat, self.Low)
        decimal_separator = locale.localeconv()["decimal_point"]
        # extract decimal digits
        decimal_part = low_str[low_str.find(decimal_separator):]
        
        # format integer part
        # 25-decimals gives 18..14 pre-decimal places
        high_str = f"{self.High:{25-decimals}d}"
        # result
        result = high_str + decimal_part
        
        # special case: add '-', if High==0, but Low<0
        if self.High == 0 and self.Low < 0:
            # '-' at position 24-decimals
            result_list = list(result)
            result_list[23-decimals] = '-'
            result = ''.join(result_list)
        
        # Shorten strings that are too long to 26 characters
        if len(result) > 26:
            result = result[:26]
        
        return result


#-------------------------------------------------------------------------
# KK_Report c_types
#-------------------------------------------------------------------------

KK_REPORT_MAX_BUFFER_SIZE = 512
# Longest measurement has 24*16 = 384 Bytes, which is also sufficient for texts

KK_REPORT_CONTENT_SIZE = KK_REPORT_MAX_BUFFER_SIZE - 1 - 1 - 2 - 4 - 8

C_ReportContentType = c_byte * KK_REPORT_CONTENT_SIZE

KK_REPORT_INDEX_DECIMALS = KK_MAX_CHANNELS*16   # SizeOf(Phase)

C_ReportDecimalsType = c_byte * KK_MAX_CHANNELS

class C_Report_Error_Code(Enum):
  CKK_DLL_Error               = 0
  CKK_DLL_NoError             = 1

  CKK_DLL_WriteError          = 3
  CKK_DLL_ServerDownError     = 4
  
  CKK_DLL_BufferTooSmall      = 6
  CKK_DLL_DeviceNotConnected  = 7
  CKK_DLL_BufferOverflow      = 8
  CKK_DLL_HardwareFault       = 9
  CKK_DLL_SourceNotFound      = 10
  CKK_DLL_CmdIgnored          = 11
  CKK_DLL_NotSupported        = 12
  CKK_DLL_Reconnected         = 13
  CKK_DLL_Recovered           = 14
  CKK_DLL_EXCEPTION           = 15


class C_ReportStruct(Structure):
    _fields_ = [('ReportType', c_byte),
                ('ErrCode', c_byte),
                ('Header', c_uint16),
                ('Len', c_int32),
                ('DeviceMs', c_int64),
                ('Content', C_ReportContentType)
    ]


#-------------------------------------------------------------------------
# KK_Report: helper classes for get_kk_report
#-------------------------------------------------------------------------

class KK_ReportType(Enum):
    RT_NONE = 0

    RT_ERROR = 1

    RT_MESSAGE = 2

    RT_PHASE = 3 # FXE measurement Phase (c_int64, c_double)

    RT_DOUBLE = 4 # FXE measurement Double (c_double)

    RT_INT32 = 5 # 32 bit integer, e.g. FXE2 heartbeat


class KK_Report:

    #-------------------------------------------------------------------------
    # private variables
    #-------------------------------------------------------------------------

    _intern = None
    _channelCount = 0
    _content = None
    c_decimals = None
    _dbg_str = None
    _error_code = ErrorCode.KK_NO_ERR
    _floats = []
    _ints = []
    _log_str = None
    _phases = []


    #-------------------------------------------------------------------------
    # constructor
    #-------------------------------------------------------------------------

    # class init
    def __init__(self, report_struct: C_ReportStruct):
        # _intern
        self._intern = report_struct
        # _error_code
        kkErrorCode = C_Report_Error_Code(self._intern.ErrCode)
        if kkErrorCode == C_Report_Error_Code.CKK_DLL_NoError:
            self._error_code = ErrorCode.KK_NO_ERR
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_WriteError:
            self._error_code = ErrorCode.KK_ERR_WRITE
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_ServerDownError:
            self._error_code = ErrorCode.KK_ERR_SERVER_DOWN
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_DeviceNotConnected:
            self._error_code = ErrorCode.KK_ERR_DEVICE_NOT_CONNECTED
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_BufferOverflow:
            self._error_code = ErrorCode.KK_ERR_BUFFER_OVERFLOW
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_HardwareFault:
            self._error_code = ErrorCode.KK_HARDWARE_FAULT
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_CmdIgnored:
            self._error_code = ErrorCode.KK_CMD_IGNORED
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_Reconnected:
            self._error_code = ErrorCode.KK_ERR_RECONNECTED
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_Recovered:
            self._error_code = ErrorCode.KK_ERR_RECONNECTED
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_EXCEPTION:
            self._error_code = ErrorCode.KK_DLL_EXCEPTION
        elif kkErrorCode == C_Report_Error_Code.CKK_DLL_SourceNotFound:
            self._error_code = ErrorCode.KK_PARAM_ERROR
        else:
            self._error_code = ErrorCode.KK_ERR
        # Measurements
        self._channelCount = 0
        self._floats.clear()
        self._ints.clear()
        self._phases.clear()
        self._content = None
        # decimal places
        self.c_decimals = None
        # Strings
        self._dbg_str = None
        self._log_str = None
        rt = self.get_report_type()
        self.get_channel_count()    # sets self._channelCount
        ba = self.get_content()
        offset = 0
        if (rt == KK_ReportType.RT_PHASE) or (rt == KK_ReportType.RT_DOUBLE):
            for i in range(self._channelCount):
                chunk = ba[offset : offset + 8]
                offset += 8
                if rt  == KK_ReportType.RT_DOUBLE:
                    self._floats.append(struct.unpack('d', chunk)[0])
                else:
                    aHigh = int.from_bytes(chunk, byteorder='little')
                    chunk = ba[offset : offset + 8]
                    offset += 8
                    aLow = struct.unpack('d', chunk)[0]
                    aPhase = Phase(aHigh, aLow)
                    self._phases.append(aPhase)

        if rt == KK_ReportType.RT_INT32:
            for i in range(self._channelCount):
                chunk = ba[offset : offset + 4]
                offset += 4
                self._ints.append(int.from_bytes(chunk, byteorder='little'))


    #-------------------------------------------------------------------------
    # getter
    #-------------------------------------------------------------------------

    def get_channel_count(self) -> int:
        """provides number of measured values (=channels) in content or 0 
        if Len/KK_ReportType is invalid
        """
        if self._channelCount == 0:
            rt = self.get_report_type()
            size = 0
            if rt == KK_ReportType.RT_PHASE:
                size = 16
            elif rt == KK_ReportType.RT_DOUBLE:
                size = 8
            elif rt == KK_ReportType.RT_INT32:
                size = 4
            if size > 0:
                self._channelCount = self._intern.Len // size
            if size * self._channelCount != self._intern.Len:
                self._channelCount = 0  # invalid Len
        return self._channelCount

    def get_content(self) -> bytearray:
        """Returns content part of KK_Report"""
        if self._content == None:
            if self._intern.Len == 0:
                self._content = None
            else:
                self._content = b''
                for i in range(self._intern.Len):
                    self._content += c_byte(self._intern.Content[i])
        return self._content

    def get_content_uint16(self) -> int:
        """The first 2 bytes of content are interpreted as a 2 byte unsigned 
        integer with HighByteFirst"""
        if self._intern.Len < 2:
            return 0xFFFF
        else:
            ba = b''
            for i in range(2):
                ba += c_byte(self._intern.Content[i])
            i = int.from_bytes(ba, byteorder='big')
            return i

    def get_content_uint32(self) -> int:
        """The first 4 bytes of content are interpreted as a 4 byte unsigned 
        integer with HighByteFirst"""
        if self._intern.Len < 4:
            return 0xFFFFFFFF
        else:
            ba = b''
            for i in range(4):
                ba += c_byte(self._intern.Content[i])
            i = int.from_bytes(ba, byteorder='big')
            return i

    def get_decimals(self) -> bytearray:
        """Returns decimals part of KK_Report, if FXE measurement"""
        if self.c_decimals == None:
            rt = self.get_report_type()
            if (rt == KK_ReportType.RT_DOUBLE) or (rt == KK_ReportType.RT_PHASE):
                self.c_decimals = b''
                for i in range(KK_MAX_CHANNELS):
                    self.c_decimals += c_byte(self._intern.Content[ \
                        i+KK_REPORT_INDEX_DECIMALS])
        return self.c_decimals

    def get_device_ms(self) -> int:
        """KK device counts ms since power up or re sync"""
        if self.get_report_type() == KK_ReportType.RT_NONE:
            return -1
        else:
            return self._intern.DeviceMs

    def get_device_ms_str(self) -> str:
        ms = self.get_device_ms()
        if ms == -1:
            return ""
        else:
            #return " ms="+f"{ms:n}"
            return " ms="+str(ms)

    def get_error_code(self) -> ErrorCode:
        """Returns ErrorCode if KK_ReportType is RT_ERROR"""
        if self.get_report_type() == KK_ReportType.RT_ERROR:
            return self._error_code
        else:
            return ErrorCode.KK_NO_ERR

    def get_error_text(self) -> str:
        """Returns Error message if KK_ReportType is RT_ERROR"""
        if self.get_report_type() == KK_ReportType.RT_ERROR:
            ba = self.get_content()
            if ba == None:
                return None
            else:
                return ba.decode()
        else:
            return None

    def get_header(self) -> int:
        """Returns report header or -1 if KK_ReportType is RT_None, RT_Error"""
        rt = self.get_report_type()
        if (rt == KK_ReportType.RT_NONE) or (rt == KK_ReportType.RT_ERROR):
            return -1
        else:
            return self._intern.Header

    def get_measurement_str(self) -> str:
        rt = self.get_report_type()
        s = ""
        if rt == KK_ReportType.RT_PHASE:
            s = "Phases: "
            for i in range(len(self._phases)):
                aPhase = self._phases[i]
                aFloat = aPhase.to_float()  # Float with loss of precision
                s += str(aFloat)+";"
        elif rt == KK_ReportType.RT_DOUBLE:
            s = "Doubles: "
            for i in range(len(self._floats)):
                aFloat = self._floats[i]
                s += str(aFloat)+";"
        elif rt == KK_ReportType.RT_INT32:
            s = "Int32: "
            for i in range(self._channelCount):
                aInt = self._ints[i]
                s += hex(aInt)+";"
        return s

    def get_message(self) -> str:
        """Returns message text if KK_ReportType is RT_MESSAGE"""
        rt = self.get_report_type()
        if rt != KK_ReportType.RT_MESSAGE:
            return None
        else:
            ba = self.get_content()
            if ba == None:
                return None
            else:
                return ba.decode()

    def get_phases(self) -> list[Phase]:
        """delivers list of received phase values"""
        if self.get_report_type() == KK_ReportType.RT_PHASE:
            return self._phases
        else:
            return []

    def get_report_type(self) -> KK_ReportType:
        """Returns KK_ReportType"""
        return KK_ReportType(self._intern.ReportType)

    def get_timestamp(self) -> int:
        """Returns timestamp value (100ms counter) if this is a timestamp report"""
        if (self._intern.Header == 0x7015) or (self._intern.Header == 0x7016):
            t = self.get_content_uint32()
            if self._intern.Header == 0x7015:
                t *= 10 # s -> 100ms
            return t
        else:
            return -1

    def is_timestamp(self) -> bool:
        """Returns this is a timestamp report"""
        return (self._intern.Header == 0x7015) or (self._intern.Header == 0x7016)


    #-------------------------------------------------------------------------
    # string conversion
    #-------------------------------------------------------------------------

    def to_dbg_str(self) -> str:
        if self._dbg_str == None:
            rt = self.get_report_type()
            if rt == KK_ReportType.RT_NONE:
                self._dbg_str = None
            elif rt == KK_ReportType.RT_ERROR:
                self._dbg_str = rt.name+" "+self.get_error_code().name \
                +": "+self.get_error_text()
            else:
                self._dbg_str = rt.name+self.get_device_ms_str() \
                    +" header=0x%04x" % self._intern.Header \
                    +" len="+str(self._intern.Len)+" "
                if rt == KK_ReportType.RT_MESSAGE:
                    if self._intern.Len == 2:
                        self._dbg_str += "0x%04x" % self.get_content_uint16()
                    elif self._intern.Len == 4:
                        self._dbg_str += "0x%08x" % self.get_content_uint32()
                    else:
                        if self._intern.Header in [0x7001, 0x7022, 0x7030, 0x7902, 0x7F23, 0x7F40]:
                            self._dbg_str += self.get_message()
                        elif self._intern.Header >= 0x7FF0:
                            self._dbg_str += self.get_message()
                        else:
                            s = ''.join(format(x, '02x') for x in self.get_content())
                            self._dbg_str += s
                else:
                    self._dbg_str += " "+self.get_measurement_str()

        return self._dbg_str

    def to_log_str(self) -> str:
        if self._log_str == None:
            rt = self.get_report_type()
            if rt == KK_ReportType.RT_NONE:
                self._log_str = None
            elif rt == KK_ReportType.RT_ERROR:
                self._log_str = "ERROR "+self.get_error_code().name \
                    +": "+self.get_error_text()
            else:
                self._log_str = "%04x" % self._intern.Header
                # version report without "; "
                if self._intern.Header != 0x7001:
                    self._log_str += "; "
                if rt == KK_ReportType.RT_MESSAGE:
                    content_int = 0
                    if self._intern.Len == 2:
                        content_int = self.get_content_uint16()
                    elif self._intern.Len == 4:
                        content_int = self.get_content_uint32()

                    if self._intern.Header in [0x7001, 0x7022, 0x7030, 0x7902, 0x7F23, 0x7F40]:
                        self._log_str += self.get_message()
                    elif self._intern.Header == 0x7015:
                        content_int *= 10 # s -> 100ms
                        self._log_str += str(content_int)
                    elif self._intern.Header == 0x7016:
                        self._log_str += str(content_int)
                    elif self._intern.Header >= 0x7FF0:
                        self._log_str += self.get_message()
                    elif self._intern.Len == 2:
                        self._log_str += "0x%04x" % content_int
                    elif self._intern.Len == 4:
                        self._log_str += "0x%08x" % content_int
                    else:
                        s = ''.join(format(x, '02x') for x in self.get_content())
                        self._log_str += s
                elif rt == KK_ReportType.RT_INT32:
                    s = ""
                    for i in range(self._channelCount):
                        aInt = self._ints[i]
                        if i > 0:
                            s +=";"
                        s += hex(aInt)
                    self._log_str += s
                elif rt == KK_ReportType.RT_DOUBLE:
                    s = ""
                    self.get_decimals()
                    for i in range(self._channelCount):
                        aFloat = self._floats[i]
                        if i > 0:
                            s +=";"
                        s += str(aFloat)
                    self._log_str += s
                elif rt == KK_ReportType.RT_PHASE:
                    s = ""
                    self.get_decimals()
                    for i in range(self._channelCount):
                        aPhase = self._phases[i]
                        if i > 0:
                            s +=";"
                        s += aPhase.to_str26(self.c_decimals[i])
                    self._log_str += s

        return self._log_str


#-------------------------------------------------------------------------
# Library class
#-------------------------------------------------------------------------

class NativeLib:
    """Wrapper class to access native K+K Library on Windows and Linux
    Needs minimum version 20.2 of K+K Library, throws NativeLibError else
    """
    
    #-------------------------------------------------------------------------
    # private variables
    #-------------------------------------------------------------------------

    _kkdll = None
    _buffer = bytearray(1024)
    _libname = ""
    

    #-------------------------------------------------------------------------
    # constructor
    #-------------------------------------------------------------------------

    # load native library
    def _load_library(self):
        # different library names for windows and linux
        self._libname = ""
        _bits = ''
        _linkage = ''
        (_bits, _linkage) = platform.architecture()
        _machine = platform.machine()
        
        if platform.system() == "Windows":
            # Windows
            if _bits.startswith('64'):
                self._libname = "KK_Library_64.dll"
            else:
                self._libname = "KK_FX80E.dll"
        else:
            # Linux
            if _machine == "aarch64":
                self._libname = "libkk_linux_aarch64_cdecl.so"
            elif _machine.startswith('arm'):
                self._libname = "libkk_linux_arm32_cdecl.so"
            elif _bits.startswith('64'):
                self._libname = "libkk_library_64_cdecl.so"
            else:
                self._libname = "libkk_library_32_cdecl.so"

        # load K+K library
        if self._libname == "":
            raise NativeLibError("Unsupported platform: "+platform.system())
        else:
            try:
                if platform.system() == "Windows":
                    # library exports functions as stdcall -> use ctypes.windll
                    return ctypes.windll.LoadLibrary(self._libname)
                else:
                    # library exports functions as cdecl -> use ctypes.cdll
                    return ctypes.cdll.LoadLibrary(self._libname)
            except OSError as exc:
                raise NativeLibError("loading K+K library ("+self._libname
                                     +") failed: "+str(exc))
    
    # class init
    def __init__(self):
        self._kkdll = self._load_library()
        # check version minimum 21.0.0 required
        version = self.get_version()
        errorVersion = "Invalid version "+version+", needs 21.1.0"
        subVersion = version
        indexdot = subVersion.find('.')
        if indexdot < 0:
            verNum = int(subVersion)
        else:
            verNum = int(subVersion[:indexdot])
        if verNum < 20:
            raise NativeLibError(errorVersion)
        doCheck = (verNum == 21)
        if doCheck:
            subVersion = subVersion[indexdot+1:]
            indexdot = subVersion.find('.')
            if indexdot < 0:
                verNum = int(subVersion)
            else:
                verNum = int(subVersion[:indexdot])
            if verNum < 1:
                raise NativeLibError(errorVersion)
            #doCheck = (verNum == 2)
            doCheck = False
        if doCheck:
            subVersion = subVersion[indexdot+1:]
            # separator blank!
            indexdot = subVersion.find(' ')
            if indexdot < 0:
                verNum = int(subVersion)
            else:
                verNum = int(subVersion[:indexdot])
            if verNum < 6:
                raise NativeLibError(errorVersion)
        # set default locale
        locale.setlocale(locale.LC_ALL, '')
         
    def get_lib_name(self) -> str:
        return self._libname
    

    #-------------------------------------------------------------------------
    # Multiple source connections
    #-------------------------------------------------------------------------
    
    def get_source_id(self) -> int:
        """Get new source id from native K+K library.
        For details see CreateMultiSource in K+K library manual.
        """
        sourceId = c_int32(self._kkdll.CreateMultiSource())
        return sourceId.value
        
        
    #-------------------------------------------------------------------------
    # Enumerate
    #-------------------------------------------------------------------------
    
    ENUM_FLAG_SERIAL_PORTS = 1
    # enumerate serial ports only
    
    ENUM_FLAG_USB = 2
    # enumerate K+K devices found on USB
    
    ENUM_FLAG_LOCAL_DEVICES = 3
    # enumerate serial ports and K+K devices on USB
     
    def enumerate_Devices(self, enum_flags: int) -> KK_Result:
        """Enumerates serial ports and/or K+K devices found on USB, depending on
        enum_flags.
        Found serial port names and USB devices names are returned in KK_Result.data
        separated by comma (,). 
        When no names are found KK_Result.data is None.
        If an error occurs, KK_Result.data contains error message.
        For details see Multi_EnumerateDevices in K+K library manual.
        @param enumFlags specifies what is to be enumerated: ENUM_FLAG_SERIAL_PORTS,
        ENUM_FLAG_USB, ENUM_FLAG_LOCAL_DEVICES 
        @return see KK_Result.result_code
        KK_NO_ERR: KK_Result.data contains found names separated by comma (,) or
        is None, if no serial ports and/or USB devices were found.
        all other codes are error numbers and KK_Result.data contains 
        error message:
        ENUM_FLAG_SERIAL_PORTS, ENUM_FLAG_USB, ENUM_FLAG_LOCAL_DEVICES: specified
        enumeration failed
        """
        # check parameter values
        kkres = KK_Result()
        if (enum_flags < 0) or (enum_flags > 3):
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid enum_Flags, must be 1..3"
            return kkres
        flags_ = c_byte(enum_flags)
        # mutable 1024 bytes buffer needed
        char_array = ctypes.c_char * len(self._buffer)
        retI = c_int32(self._kkdll.Multi_EnumerateDevices(
                char_array.from_buffer(self._buffer), flags_))
        if retI.value == 0: 
            kkres.data = bytearray2string(self._buffer)
        else:
            # get error message
            self._kkdll.Multi_GetEnumerateDevicesErrorMsg.restype = c_char_p
            retS = c_char_p(self._kkdll.Multi_GetEnumerateDevicesErrorMsg())
            kkres.data = retS.value.decode()
            if retI.value == -1:
                kkres.result_code = ErrorCode.KK_ERR_ENUM_SERIAL
            elif retI.value == -2:
                kkres.result_code = ErrorCode.KK_ERR_ENUM_USB
            else:
                kkres.result_code = ErrorCode.KK_ERR_ENUM_SERIAL_USB
        return kkres

    def get_host_and_IPs(self, l: list[str] = None) -> KK_Result:
        """Enumerates local IPv4 addresses and host name of local computer and
        delivers them in list l.
        If an error occurs KK_Result.data contains error message. Error could be
        on host name (no strings appended to l) or IP addresses (only host name
        is appended to l).
        For details see Multi_GetHostAndIPs in K+K library manual.
        @param l: list which host name and IP addresses are appended
        @return see KK_Result.result_code
        KK_NO_ERR: strings are appended to l: 
        first string: host, 
        next strings: one per IP address
        KK_ERR_BUFFER_TOO_SMALL: strings are appended to l, but truncated to 
        80 characters
        KK_ERR: operation fails, KK_Result.data contains error message
        """
        if l is None:
            l = []
        host = bytearray(80)
        ahost = ctypes.c_char * len(host)
        ips = bytearray(80)
        aips = ctypes.c_char * len(ips)
        error = bytearray(80)
        aerror = ctypes.c_char * len(error)
        retI = c_int32(self._kkdll.Multi_GetHostAndIPs(
                ahost.from_buffer(host), 
                aips.from_buffer(ips), 
                aerror.from_buffer(error)))
        kkres = KK_Result()
        if retI.value == 0:
            #error
            kkres.data = error.decode()
            kkres.result_code = ErrorCode.KK_ERR
        elif retI.value == 6:
            kkres.result_code = ErrorCode.KK_ERR_BUFFER_TOO_SMALL
        if host[0] != 0:
            l.append(bytearray2string(host))
            # ips only if host present
            if ips[0] != 0:
                # split string after comma
                s = bytearray2string(ips)
                while s is not None:
                    try:
                        i = s.index(',')
                        sub = s[:i]
                        l.append(sub)
                        s = s[i+1:]
                    except ValueError:
                        # ',' not found -> append last string
                        l.append(s)
                        s = None
        return kkres
            
    
    #-------------------------------------------------------------------------
    # Output path
    #-------------------------------------------------------------------------
    
    def get_output_path(self, source_id: int) -> KK_Result:
        """Get current output path for files generated by library 
        (test data, debug log) for source_id.
        For details see Multi_GetOutputPath in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: see KK_Result.result_code
        KK_NO_ERR: KK_Result.data contains current output path
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains 
        error message.
        """
        id_ = c_int32(source_id)
        self._kkdll.Multi_GetOutputPath.restype = c_char_p
        retS = c_char_p(self._kkdll.Multi_GetOutputPath(id_))
        kkres = KK_Result()
        kkres.data = retS.value.decode('ascii')
        if kkres.data.startswith(_ERR_SOURCE_NOT_FOUND):
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        return kkres 
        
    def set_output_path(self, source_id: int, path: str) -> KK_Result:
        """Set actual library output path used for debug log or test data
        for source_id.
        For details see Multi_SetOutputPath in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param path: new library output path
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains 
        error message.
        KK_Err: call failed, KK_Result.data contains error message
        """
        id_ = c_int32(source_id)
        dllPath = path.encode('ascii')
        self._kkdll.Multi_SetOutputPath.restype = c_char_p
        retS = c_char_p(self._kkdll.Multi_SetOutputPath(id_, dllPath))
        kkres = KK_Result()
        if retS.value is not None:
            # error
            kkres.data = retS.value.decode('ascii')
            if kkres.data.startswith(_ERR_SOURCE_NOT_FOUND):
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
            else:
                kkres.result_code = ErrorCode.KK_ERR
        return kkres 
    
    
    #-------------------------------------------------------------------------
    # Debug log
    #-------------------------------------------------------------------------
    
    def get_debug_filename(self, source_id: int) -> KK_Result:
        """Get current file name of debug log file for source_id
        For details see Multi_DebugGetFilename in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: see KK_Result.result_code
        KK_NO_ERR: KK_Result.data contains file name, maybe None,
        if no debug log file is opened
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains 
        error message.
        """
        id_ = c_int32(source_id)
        self._kkdll.Multi_DebugGetFilename.restype = c_char_p
        retS = c_char_p(self._kkdll.Multi_DebugGetFilename(id_))
        kkres = KK_Result()
        if retS.value is not None:
            # debug file name or error
            kkres.data = retS.value.decode('ascii')
            if kkres.data.startswith(_ERR_SOURCE_NOT_FOUND):
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
        return kkres 
        
    def set_debug_log(self, source_id: int, dbg_on: bool, 
                      dbg_id: str, error_only: bool) -> KK_Result:
        """Opens or closes debug log file of native K+K library for 
        source_id. Every call to a library function generates log outputs 
        to the debug file, if file is open.
        For details see Multi_Debug, Multi_Debug_ErrorOnly in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param dbg_on: True opens, False closes debug log file
        @param dbg_id: source identifier of debug log file, part of file name
        @param error_only: only keep debug files with errors and their
        predecessors
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains 
        error message.
        KK_Err: call failed, KK_Result.data contains error message
        """
        id_ = c_int32(source_id)
        ascii_id = None
        if dbg_id is not None:
            ascii_id = dbg_id.encode('ascii')
        if error_only:
            self._kkdll.Multi_Debug_ErrorOnly.restype = c_char_p
            retS = c_char_p(self._kkdll.Multi_Debug_ErrorOnly(id_, dbg_on, ascii_id))
        else:
            self._kkdll.Multi_Debug.restype = c_char_p
            retS = c_char_p(self._kkdll.Multi_Debug(id_, dbg_on, ascii_id))
        kkres = KK_Result()
        if retS.value is not None:
            # error
            kkres.data = retS.value.decode('ascii')
            if kkres.data.startswith(_ERR_SOURCE_NOT_FOUND):
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
            else:
                kkres.result_code = ErrorCode.KK_ERR
        return kkres 
    
    def set_debug_stream(self, source_id: int, dbg_on: bool, 
                      dbg_id: str, error_only: bool) -> KK_Result:
        """Opens or closes debug stream log file of native K+K library for 
        source_id. Logs received bytes and sent commands, if file is open.
        For details see Multi_DebugStream in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param dbg_on: True opens, False closes debug stream log file
        @param dbg_id: source identifier of debug stream log file,
        part of file name
        @param error_only: only keep debug files with errors and their
        predecessors
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains 
        error message.
        KK_Err: call failed, KK_Result.data contains error message
        """
        id_ = c_int32(source_id)
        ascii_id = None
        if dbg_id is not None:
            ascii_id = dbg_id.encode('ascii')
        err_flag = ctypes.c_bool(error_only)
        self._kkdll.Multi_DebugStream.restype = c_char_p
        retS = c_char_p(self._kkdll.Multi_DebugStream(id_, dbg_on, ascii_id, err_flag))
        kkres = KK_Result()
        if retS.value is not None:
            # error
            kkres.data = retS.value.decode('ascii')
            if kkres.data.startswith(_ERR_SOURCE_NOT_FOUND):
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
            else:
                kkres.result_code = ErrorCode.KK_ERR
        return kkres 
    
    def set_debug_flags(self, source_id: int, report_log: bool, 
                        low_level_log: bool) -> KK_Result:
        """Sets extent of debug log file for source_id.
        For details see Multi_DebugFlags in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param report_Log generate log output about received reports
        @param low_Level_Log generate log output about byte stream 
        received from/send to K+K device or K+K server.
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains 
        error message.
        """
        id_ = c_int32(source_id)
        rep = ctypes.c_bool(report_log)
        low = ctypes.c_bool(low_level_log)
        retI = c_int32(self._kkdll.Multi_DebugFlags(id_, rep, low))
        kkres = KK_Result()
        if retI.value == 10:
            kkres.data = "Invalid parameter source_id: "+str(source_id)
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        return kkres
        
    def set_debug_log_limit(self, source_id: int, log_type: DebugLogType, 
                            size: int) -> KK_Result:
        """Sets limits for debug log file for source_id.
        For details see Multi_DebugLogLimit in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param log_type specifies debug file management, a value of 
        DebugLogType
        @param size maximum size of debug log file, used if 
        log_type != LOG_UNLIMITED
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        """
        # check parameter values
        kkres = KK_Result()
        if size < 0:
            kkres.data = "Invalid parameter size: "+str(size)  \
                            +", must be positive"
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
        id_ = c_int32(source_id)
        if log_type == DebugLogType.LOG_OVERWRITE:
            logt = c_byte(1)
        elif log_type == DebugLogType.LOG_CREATE_NEW:
            logt = c_byte(2)
        else:
            logt = c_byte(0)
        logs = c_uint(size)
        retI = c_int32(self._kkdll.Multi_DebugLogLimit(id_, logt, logs))
        if retI.value == 10:
            kkres.data = "Invalid parameter source_id: "+str(source_id)
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        return kkres
    
    
    #-------------------------------------------------------------------------
    # Info requests
    #-------------------------------------------------------------------------
    
    def get_version(self) -> str:
        """Get version string from native K+K library.
        For details see Multi_DebugDLLVersion in K+K library manual.
        """
        self._kkdll.Multi_GetDLLVersion.restype = c_char_p
        version = c_char_p(self._kkdll.Multi_GetDLLVersion())
        return version.value.decode('ascii')

        
    def get_buffer_amount(self, source_id: int) -> int:
        """Get count of bytes in receive buffer for source_id.
        For details see Multi_GetBufferAmount in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: count of bytes in receive buffer not read yet
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_GetBufferAmount(id_))
        return retI.value
    
    def get_transmit_buffer_amount(self, source_id: int) -> int:
        """Get count of bytes in transmit buffer of K+K device
        connected with source_id.
        For details see Multi_GetTransmitBufferAmount in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: count of bytes in transmit buffer not sent yet
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_GetTransmitBufferAmount(id_))
        return retI.value
        
    def get_user_id(self, source_id: int) -> int:
        """Get user id assigned from K+K device connected with source_id.
        For details see Multi_GetUserID in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: 1..4: user id, -1: error
        """
        id_ = c_int32(source_id)
        retI = c_byte(self._kkdll.Multi_GetUserID(id_))
        return retI.value
    
    def is_file_device(self, source_id: int) -> bool:
        """Returns True, if data is read from file for source_id.
        For details see Multi_IsFileDevice in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return True, if measurement data is read from file
        """
        id_ = c_int32(source_id)
        retI = c_bool(self._kkdll.Multi_IsFileDevice(id_))
        return retI.value
    
    def is_serial_device(self, source_id: int) -> bool:
        """Returns True, if data is read from serial connection for source_id.
        For details see Multi_IsSerialDevice in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return True, if measurement data is read from serial connection
        """
        id_ = c_int32(source_id)
        retI = c_bool(self._kkdll.Multi_IsSerialDevice(id_))
        return retI.value
    
    def get_firmware_version(self, source_id: int) -> int:
        """Get firmware version number of K+K device
        connected with source_id.
        For details see Multi_GetFirmwareVersion in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: firmware version number
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_GetFirmwareVersion(id_))
        return retI.value

    def has_FRAM(self, source_id: int) -> bool:
        """Returns True, if K+K device connected with source_id is
        equipped with F-RAM memory.
        For details see Multi_HasFRAM in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return True, if K+K device is equipped with F-RAM memory.
        """
        id_ = c_int32(source_id)
        retI = c_bool(self._kkdll.Multi_HasFRAM(id_))
        return retI.value
    
    def get_device_start_state(self, source_id: int) -> int:
        """Delivers the state of the K+K device at application start.
        For details see Multi_GetDeviceStartState in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @return
        0: Source-ID invalid or no time stamp received
        1: Cold start, K+K device was restarted
        2: Warm start
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_GetDeviceStartState(id_))
        return retI.value
        
    #-------------------------------------------------------------------------
    # Calibration
    #-------------------------------------------------------------------------

    def set_NSZ_calibration_data(self, source_id: int, 
                                 calib_data: list[float]) -> KK_Result:
        """Writes NSZ calibration data to K+K device connected with source_id.
        Needs firmware version 62 or higher, not supported for serial connections.
        For details see Multi_SetNSZCalibrationData in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param calib_data: list of float with calibration value per channel
        in nanoseconds  
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_Err: command failed, KK_Result.data contains error message
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR_NOT_SUPPORTED: writing NSZ calibration data not supported
        by K+K device, KK_Result.data contains error message.
        """
        # check parameter values
        kkres = KK_Result()
        if (calib_data == None) or (calib_data == []):
            kkres.data = "Parameter calib_data missing"
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        elif len(calib_data) > KK_MAX_CHANNELS:
            kkres.data = "Invalid parameter calib_data: too many channels"
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        if kkres.result_code != ErrorCode.KK_NO_ERR:
            return kkres
            
        id_ = c_int32(source_id)
        # generate floating point string, separated by semicolon
        # with 3 digits behind decimal separator
        # use locale, set in set_decimal_separator
        s_ = ''
        for f in calib_data:
            s_ = s_ + locale.format('%.3f', f) + ';'
        # delete last ;
        s_ = s_[:-1]
        data_ = s_.encode('ascii')
        retI = c_int32(self._kkdll.Multi_SetNSZCalibrationData(id_, data_))
        if retI.value == 0:
            # check serial connection
            if self.is_serial_device(self, source_id):
                kkres.data = "Serial connection not supported"
            else:
                kkres.data = "Conversion error, set decimal separator!"
            kkres.result_code = ErrorCode.KK_ERR
        elif retI.value == 10:
            kkres.data = "Invalid parameter source_id: "+str(source_id)
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        elif retI.value == 12:
            kkres.data = "Feature not supported, needs firmware version 62 or higher"
            kkres.result_code = ErrorCode.KK_ERR_NOT_SUPPORTED
        return kkres


    #-------------------------------------------------------------------------
    # FHR settings
    #-------------------------------------------------------------------------

    def get_FHR_settings(self, source_id: int) -> KK_Result:
        """Requests FHR settings from K+K device connected with source_id.
        K+K device responds with 0x7902 report including requested data.
        Needs firmware version 67 or higher
        For details see Multi_ReadFHRData in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_Err: command failed, KK_Result.data contains error message
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR_NOT_SUPPORTED: requesting FHR settings not supported
        by K+K device, KK_Result.data contains error message.
        """
        id_ = c_int32(source_id)
        kkres = KK_Result()
        retI = c_int32(self._kkdll.Multi_ReadFHRData(id_))
        if retI.value == 0:
            kkres.data = "Command failed"
            kkres.result_code = ErrorCode.KK_ERR
        elif retI.value == 10:
            kkres.data = "Invalid parameter source_id: "+str(source_id)
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        elif retI.value == 12:
            kkres.data = "Feature not supported, needs firmware version 67 or higher"
            kkres.result_code = ErrorCode.KK_ERR_NOT_SUPPORTED
        return kkres

    def set_FHR_settings(self, source_id: int,
                         fhr_data: str) -> KK_Result:
        """Writes FHR settings to K+K device connected with source_id.
        Needs firmware version 67 or higher, not supported for serial connections.
        For details see Multi_SetFHRData in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param fhr_data: string representation of FHRSettings
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_Err: command failed, KK_Result.data contains error message
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR_NOT_SUPPORTED: writing NSZ calibration data not supported
        by K+K device, KK_Result.data contains error message.
        """
        # check parameter values
        kkres = KK_Result()
        try:
            FHRSettings(fhr_data)
        except:
            kkres.data = "Invalid parameter fhr_data"
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
            
        id_ = c_int32(source_id)
        data_ = fhr_data.encode('ascii')
        retI = c_int32(self._kkdll.Multi_SetFHRData(id_, data_))
        if retI.value == 0:
            # check serial connection
            if self.is_serial_device(self, source_id):
                kkres.data = "Serial connection not supported"
            else:
                kkres.data = "Conversion error, set decimal separator!"
            kkres.result_code = ErrorCode.KK_ERR
        elif retI.value == 10:
            kkres.data = "Invalid parameter source_id: "+str(source_id)
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        elif retI.value == 12:
            kkres.data = "Feature not supported, needs firmware version 67 or higher"
            kkres.result_code = ErrorCode.KK_ERR_NOT_SUPPORTED
        return kkres
    
    
    #-------------------------------------------------------------------------
    # Open / Close connection
    #-------------------------------------------------------------------------
    
    def open_connection(self, source_id: int, connection: str, 
                        blocking_IO: bool) -> KK_Result:
        """Opens connection for source_id to K+K device or K+K server
        or read data from file according to connection.
        A previously opened connection for source_id will be closed.
        For details see Multi_OpenConnection in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param connection: String describes connection to open
        @param param blocking_IO: open connection with blocking read and 
        write operations
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR: open failed, KK_Result.data contains error message.
        KK_DLL_EXCEPTION: open failed with exception, KK_Result.data contains
        error message.
        """
        id_ = c_int32(source_id)
        conn = connection.encode('ascii')
        b_IO =  ctypes.c_bool(blocking_IO)
        # conn needs 1024 byte buffer
        for i in range(len(conn)):
            self._buffer[i] = conn[i]
        # append terminating 0
        self._buffer[len(conn)] = 0
        # mutable variable needed
        char_array = ctypes.c_char * len(self._buffer)
        kkres = KK_Result()
        retI = c_int32(self._kkdll.Multi_OpenConnection(
                id_, char_array.from_buffer(self._buffer), b_IO))
        if retI.value != 1:
            # buffer contains error message
            kkres.data = bytearray2string(self._buffer)
            if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
            elif retI.value == 15:
                kkres.result_code = ErrorCode.KK_DLL_EXCEPTION
            else:
                kkres.result_code = ErrorCode.KK_ERR
        
        return kkres
        
    def close_connection(self, source_id: int):
        """Closes a previously opened connection for source_id.
        For details see Multi_CloseConnection in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        """
        id_ = c_int32(source_id)
        self._kkdll.Multi_CloseConnection(id_)
        
    
    #-------------------------------------------------------------------------
    # Get report
    #-------------------------------------------------------------------------
    
    def set_decimal_separator(self, source_id: int, 
                              separator: str) -> KK_Result:
        """Sets decimal separator used to convert floating point numbers
        into string.
        For details see Multi_SetDecimalSeparator in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param separator: must be '.' or ',' 
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        """
        # check parameter values
        kkres = KK_Result()
        if (separator != '.') and (separator != ','):
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter separator: "+separator
            return kkres
        # set locale for conversion string <-> float
        try:
            if separator == '.':
                locale.setlocale(locale.LC_NUMERIC, locale.normalize('en_US'))
            else:
                locale.setlocale(locale.LC_NUMERIC, locale.normalize('de_DE'))
        except:
            pass
        
        id_ = c_int32(source_id)
        b = bytes(separator, 'ascii')
        sep = c_char(b[0])
        retI = c_int32(self._kkdll.Multi_SetDecimalSeparator(id_, sep))
        if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
                kkres.data = "Invalid parameter source_id: "+str(source_id)
        return kkres        
    
    def set_NSZ(self, source_id: int, aNSZ: int) -> KK_Result:
        """Sets count of NSZ measurements sent by K+K measurement card.
        For details see Multi_SetNSZ in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param aNSZ: must be 1 or 2 
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        """
        # check parameter values
        kkres = KK_Result()
        if (aNSZ != 1) and (aNSZ != 2):
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter aNSZ: "+str(aNSZ)
            return kkres
        id_ = c_int32(source_id)
        nsz = c_int32(aNSZ)
        retI = c_int32(self._kkdll.Multi_SetNSZ(id_, nsz))
        if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
                kkres.data = "Invalid parameter source_id: "+str(source_id)
        return kkres
    
    def get_report(self, source_id: int) -> KK_Result:
        """Gets next report for source_id received via current connection from
        K+K device or K+K server or read from file and write it to KK_Result.data.
        For details see Multi_GetReport in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @return: see KK_Result.result_code
        KK_NO_ERR: operation successful, KK_Result.data contains report or
        None, if no report available
        KK_ERR_BUFFER_TOO_SMALL: operation successful, but string in 
        KK_Result.data is truncated to 1024 characters.
        all other codes are error numbers and KK_Result.data contains 
        error message:
        KK_PARAM_ERROR: invalid source_id
        KK_ERR_WRITE: Writing to device/server failed. Connection is closed.
        For details see Multi_SendCommand in K+K library manual. 
        KK_ERR_SERVER_DOWN: K+K TCP server is not online. Connection is closed.
        KK_ERR_DEVICE_NOT_CONNECTED: No connection to K+K device (connection has
        not been established or usb cable is unplugged) 
        KK_ERR_BUFFER_OVERFLOW: Data was lost. App does not read fast enough.
        KK_HARDWARE_FAULT: Fault in measurement hardware. Connection to
        K+K device is closed.
        KK_ERR_RECONNECTED: A previously interrupted connection is reconnectd now
        with data loss.
        KK_DLL_EXCEPTION: exception error
        """
        id_ = c_int32(source_id)
        # mutable 1024 bytes buffer needed
        char_array = ctypes.c_char * len(self._buffer)
        # get log report
        retI = c_int32(self._kkdll.Multi_GetReport(id_, 
                        char_array.from_buffer(self._buffer)))
        kkres = KK_Result()
        # convert to string
        kkres.data = bytearray2string(self._buffer)
        if retI.value == 6:
            kkres.result_code = ErrorCode.KK_ERR_BUFFER_TOO_SMALL
        elif retI.value == 0:
            kkres.result_code = ErrorCode.KK_ERR
        elif retI.value == 3:
            kkres.result_code = ErrorCode.KK_ERR_WRITE
        elif retI.value == 4:
            kkres.result_code = ErrorCode.KK_ERR_SERVER_DOWN
        elif retI.value == 7:
            kkres.result_code = ErrorCode.KK_ERR_DEVICE_NOT_CONNECTED
        elif retI.value == 8:
            kkres.result_code = ErrorCode.KK_ERR_BUFFER_OVERFLOW
        elif retI.value == 9:
            kkres.result_code = ErrorCode.KK_HARDWARE_FAULT
        elif retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        elif retI.value == 13:
            kkres.result_code = ErrorCode.KK_ERR_RECONNECTED
        elif retI.value == 15:
                kkres.result_code = ErrorCode.KK_DLL_EXCEPTION
        return kkres
    
    def get_kk_report(self, source_id: int) -> KK_Report:
        id_ = c_int32(source_id)
        # get report
        c_report_struct = C_ReportStruct()
        retI = c_int32(self._kkdll.Multi_GetReportStruct(id_, 
                        byref(c_report_struct)))
        return KK_Report(c_report_struct)
    

    def set_send_7016(self, source_id: int, value: bool) -> KK_Result:
        """The 100ms timestamps received from the K+K device are passed
        on to the application by default with Report 7000.
        Here it is now possible to switch to Report 7016.
        For details see Multi_SetSend7016 in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param value:
        True: send Report 7016
        False: send Report 7000 (Default)
        @return: see KK_Result.result_code
        KK_NO_ERR: operation successful
        KK_PARAM_ERROR: invalid source_id
        """
        id_ = c_int32(source_id)
        val = c_bool(value)
        kkres = KK_Result()
        retI = c_int32(self._kkdll.Multi_SetSend7016(id_, val))        
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        return kkres

        
    #-------------------------------------------------------------------------
    # Send command
    #-------------------------------------------------------------------------
    
    def get_pending_commands_count(self, source_id: int) -> int:
        """Gets count of buffered (not yet sent) commands for source_id.
        For details see Multi_GetPendingCmdsCount in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: count of waiting commands
        """
        id_ = c_int32(source_id)
        retI = c_byte(self._kkdll.Multi_GetPendingCmdsCount(id_))
        return retI.value
    
    def set_command_limit(self, source_id: int, limit: int) -> KK_Result:
        """Sets limit of command waiting queue for source_id to limit.
        For an unlimited waiting queue set limit to 0.
        For details see Multi_SetCommadLimit in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param limit: length of waiting queue(>=0), 0=unlimited, 
        default=unlimited 
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        """
        # check parameter values
        kkres = KK_Result()
        if limit < 0:
            kkres.data = "Invalid parameter limit: "+str(limit)
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
        id_ = c_int32(source_id)
        lim = c_int32(limit)
        retI = c_int32(self._kkdll.Multi_SetCommandLimit(id_, lim))    
        if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
                kkres.data = "Invalid parameter source_id: "+str(source_id)
        return kkres
        
    def remote_login(self, source_id: int, password: int) -> KK_Result:
        """Remote login procedure on K+K device connected to source_id.
        For details see Multi_RemoteLogin in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param password: remote password
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        all other codes are error numbers and KK_Result.data contains 
        error message:
        KK_PARAM_ERROR: invalid parameter value
        KK_ERR: invalid password
        KK_CMD_IGNORED: command rejected
        """
        id_ = c_int32(source_id)
        password_ = c_uint32(password)
        # mutable 1024 bytes buffer needed
        char_array = ctypes.c_char * len(self._buffer)
        # get log report
        retI = c_int32(self._kkdll.Multi_RemoteLogin(id_, password_, 
                        char_array.from_buffer(self._buffer)))
        kkres = KK_Result()
        if retI.value != 1:
            # buffer contains error message
            kkres.data = bytearray2string(self._buffer)
            if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
            elif retI.value == 0:
                kkres.result_code = ErrorCode.KK_ERR
            elif retI.value == 11:
                kkres.result_code = ErrorCode.KK_CMD_IGNORED
        return kkres
    
    def send_command(self, source_id: int, command: bytes) -> KK_Result:
        """Adds command to waiting queue of source_id.
        If there is no current connection or waiting queue is full,
        command is rejected (KK_CMD_IGNORED). 
        For details see Multi_SendCommand in K+K library manual.
        @param source_id: source identifier returned by get_source_id
        @param command: command bytes to be sent
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        all other codes are error numbers and KK_Result.data contains 
        error message:
        KK_PARAM_ERROR: invalid parameter value
        KK_ERR: an error occurred
        KK_CMD_IGNORED: command rejected
        KK_DLL_EXCEPTION: exception error
        """
        # check parameter values
        kkres = KK_Result()
        if len(command) == 0:
            kkres.data = "Invalid parameter command: "+command
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
        id_ = c_int32(source_id)
        l = c_int32(len(command))
        # command needs 1024 byte buffer
        for i in range(len(command)):
            self._buffer[i] = command[i]
        # mutable variable needed
        char_array = ctypes.c_char * len(self._buffer)
        retI = c_int32(self._kkdll.Multi_SendCommand(
                id_, char_array.from_buffer(self._buffer), l))
        if retI.value != 1:
            # buffer contains error message
            kkres.data = bytearray2string(self._buffer)
            if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
            elif retI.value == 0:
                kkres.result_code = ErrorCode.KK_ERR
            elif retI.value == 11:
                kkres.result_code = ErrorCode.KK_CMD_IGNORED
            elif retI.value == 15:
                kkres.result_code = ErrorCode.KK_DLL_EXCEPTION
        return kkres
    
    
    #-------------------------------------------------------------------------
    # Local TCP server
    #-------------------------------------------------------------------------
    
    def start_TCP_server(self, source_id: int, aPort: int) -> KK_Result:
        """Starts local TCP server for source_id on port aPort.
        If aPort == 0, port number is assigned by system and returned 
        in KK_Result.intValue
        For details see Multi_StartTcpServer in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param aPort port number for TCP server, system assigned value
        see KK_Result.intValue
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR: operation failed, KK_Result.data contains error message.
        """
        id_ = c_int32(source_id)
        port_ = c_int32(aPort)
        kkres = KK_Result()
        retI = c_int32(self._kkdll.Multi_StartTcpServer(id_, byref(port_)))
        if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
                kkres.data = "Invalid parameter source_id: "+str(source_id)
        elif retI.value == 0:
                kkres.result_code = ErrorCode.KK_ERR
                self._kkdll.Multi_GetTcpServerError.restype = c_char_p
                error = c_char_p(self._kkdll.Multi_GetTcpServerError(id_))
                kkres.data = error.value.decode('ascii')
        else:
            kkres.int_value = port_.value
        return kkres
    
    def stop_TCP_server(self, source_id: int) -> KK_Result:
        """Stops local TCP server for source_id.
        All client connections are disconnected.
        For details see Multi_StopTcpServer in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR: operation failed, KK_Result.data contains error message.
        """
        id_ = c_int32(source_id)
        kkres = KK_Result()
        retI = c_int32(self._kkdll.Multi_StopTcpServer(id_))
        if retI.value == 10:
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
                kkres.data = "Invalid parameter source_id: "+str(source_id)
        elif retI.value == 0:
                kkres.result_code = ErrorCode.KK_ERR
                self._kkdll.Multi_GetTcpServerError.restype = c_char_p
                error = c_char_p(self._kkdll.Multi_GetTcpServerError(id_))
                kkres.data = error.value.decode('ascii')
        return kkres
    
    def report_TCP_log(self, source_id: int, data: str, logType: str) -> KK_Result:
        """Transmits log entry data for LOG level type logType to local 
        TCP server on source_id.
        Local TCP server proceeds data to connected TCP receiver clients.
        If an error occurs (source_id invalid, source without local tcp server), 
        call is ignored.
        For details see Multi_TcpReportLog in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param data Log entry to transmit to connected clients
        @param logType specifies log entry type, one of following values: 
            'PHASELOG', 'FREQLOG', 'PHASEDIFFLOG', 'NSZLOG', 'NSZDIFFLOG',
            'PHASEPREDECESSORLOG', 'USERLOG1', 'USERLOG2'
        @return: see KK_Result.result_code
        KK_NO_ERR: successful or ignored operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        """
        # check parameter values
        kkres = KK_Result()
        if data == None:
            kkres.data = "Missing parameter data"
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        data_ = data.encode('ascii')
        if logType == 'PHASELOG':
            log_ = c_int32(0)
        elif logType == 'FREQLOG':
            log_ = c_int32(1)
        elif logType == 'PHASEDIFFLOG':
            log_ = c_int32(2)
        elif logType == 'NSZLOG':
            log_ = c_int32(3)
        elif logType == 'NSZEDIFFLOG':
            log_ = c_int32(4)
        elif logType == 'PHASEPREDECESSORLOG':
            log_ = c_int32(5)
        elif logType == 'USERLOG1':
            log_ = c_int32(6)
        elif logType == 'USERLOG2':
            log_ = c_int32(7)
        else:
            kkres.data = "Invalid parameter logType: "+logType
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
        id_ = c_int32(source_id)
        self._kkdll.Multi_TcpReportLog(id_, data_, log_)
        return kkres
    
    
    #-------------------------------------------------------------------------
    # Connect to TCP server on LOG level
    #-------------------------------------------------------------------------
    
    def open_TCP_log(self, source_id: int, ip_port: str, 
                     mode: str, format_fxe: str = None) -> KK_Result:
        """Opens connection for source_id to K+K TCP server on address
        ip_Port for log level entries.
        For details see Multi_OpenTcpLog* in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param ip_Port IPaddress:port with
        IPaddress: IPv4 address of K+K TCP server, e.g. 192.168.178.98 or 
                    127.0.0.1 for local host
        port: port number (16 bit value) of TCP server.
        @param mode report mode to receive, one of following values: 
            'PHASELOG', 'FREQLOG', 'PHASEDIFFLOG', 'NSZLOG', 'NSZDIFFLOG',
            'PHASEPREDECESSORLOG', 'USERLOG1', 'USERLOG2'
        @param format_fxe: string to format FXE time stamp or None
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR: open failed, KK_Result.data contains error message.
        """
        # mode -> log_type
        kkres = KK_Result()
        log_type = -1   # invalid
        if (mode == 'PHASELOG'):
            log_type = 0
        elif (mode == 'FREQLOG'):
            log_type = 1
        elif (mode == 'PHASEDIFFLOG'):
            log_type = 2
        elif (mode == 'NSZLOG'):
            log_type = 3
        elif (mode == 'NSZDIFFLOG'):
            log_type = 4
        elif (mode == 'PHASEPREDECESSORLOG'):
            log_type = 5
        elif (mode == 'USERLOG!'):
            log_type = 6
        elif (mode == 'USERLOG2'):
            log_type = 7
        else:
            kkres.data = "Invalid parameter mode: "+mode
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
        
        kkres = self.open_TCP_log_type(source_id, ip_port, log_type, None, format_fxe)
        return kkres
        
    def open_TCP_log_time(self, source_id: int, ip_port: str, 
                     mode: str, format_str: str, format_fxe: str = None) -> KK_Result:
        """Opens connection for source_id to K+K TCP server on address
        ip_Port for log level entries.
        For details see Multi_OpenTcpLog* in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param ip_Port IPaddress:port with
        IPaddress: IPv4 address of K+K TCP server, e.g. 192.168.178.98 or 
                    127.0.0.1 for local host
        port: port number (16 bit value) of TCP server.
        @param mode report mode to receive, one of following values: 
            'PHASELOG', 'FREQLOG', 'PHASEDIFFLOG', 'NSZLOG', 'NSZDIFFLOG',
            'PHASEPREDECESSORLOG', 'USERLOG1', 'USERLOG2'
        @param format_str: string to format UTC time stamp
            e.g. 'YYYYMMDD HH:NN:SS.ZZZ'
        @param format_fxe: string to format FXE time stamp or None
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR: open failed, KK_Result.data contains error message.
        """
        # mode -> log_type
        kkres = KK_Result()
        log_type = -1   # invalid
        if (mode == 'PHASELOG'):
            log_type = 0
        elif (mode == 'FREQLOG'):
            log_type = 1
        elif (mode == 'PHASEDIFFLOG'):
            log_type = 2
        elif (mode == 'NSZLOG'):
            log_type = 3
        elif (mode == 'NSZDIFFLOG'):
            log_type = 4
        elif (mode == 'PHASEPREDECESSORLOG'):
            log_type = 5
        elif (mode == 'USERLOG!'):
            log_type = 6
        elif (mode == 'USERLOG2'):
            log_type = 7
        else:
            kkres.data = "Invalid parameter mode: "+mode
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
        
        kkres = self.open_TCP_log_type(source_id, ip_port, log_type, format_str, format_fxe)
        return kkres
        
    def open_TCP_log_type(self, source_id: int, ip_port: str, 
                     log_type: int, format_str: str,
                     format_fxe: str = None) -> KK_Result:
        """Opens connection for source_id to K+K TCP server on address
        ip_Port for log level entries.
        For details seeMulti_OpenTcpLogTypeFxe in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param ip_Port IPaddress:port with
        IPaddress: IPv4 address of K+K TCP server, e.g. 192.168.178.98 or 
                    127.0.0.1 for local host
        port: port number (16 bit value) of TCP server.
        @param log_type report mode to receive 0..7 
        @param format_str: string to format UTC time stamp or None
            e.g. 'YYYYMMDD HH:NN:SS.ZZZ'
        @param format_fxe: string to format FXE time stamp or None
        @return: see KK_Result.result_code
        KK_NO_ERR: successful operation
        KK_PARAM_ERROR: invalid parameter value, KK_Result.data contains 
        error message.
        KK_ERR: open failed, KK_Result.data contains error message.
        """
        # check parameter values
        kkres = KK_Result()
        if ((log_type < 0) or (log_type > 7)): 
            kkres.data = "Invalid parameter log_type: "+log_type
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
        if ip_port is None:
            kkres.data = "Parameter missing: ip_port"
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            return kkres
            
        id_ = c_int32(source_id)
        ip = ip_port.encode('ascii')
        l_ = c_int32(log_type)
        
        f = None
        if format_str is not None:
            f = c_char_p(format_str.encode('ascii'))
        f2 = None
        if format_fxe is not None:
            f2 = c_char_p(format_fxe.encode('ascii'))
        # ip needs 1024 byte buffer
        for i in range(len(ip)):
            self._buffer[i] = ip[i]
        # append terminating 0
        self._buffer[len(ip)] = 0
        # mutable variable needed
        char_array = ctypes.c_char * len(self._buffer)
        retI = c_int32(self._kkdll.Multi_OpenTcpLogTypeFxe(
                id_, char_array.from_buffer(self._buffer), l_, f, f2))

        if retI.value != 1:
            # buffer contains error message
            kkres.data = bytearray2string(self._buffer)
            if kkres.data.startswith(_ERR_SOURCE_NOT_FOUND):
                kkres.result_code = ErrorCode.KK_PARAM_ERROR
            else:
                kkres.result_code = ErrorCode.KK_ERR
        return kkres
        
    def close_TCP_log(self, source_id: int):        
        """Closes previously opened connection for source_id to 
        LOG level K+K TCP server.
        For details see Multi_CloseTcpLog in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        """
        id_ = c_int32(source_id)
        self._kkdll.Multi_CloseTcpLog(id_)
        
    def get_TCP_log(self, source_id: int) -> KK_Result:
        """Get next log level entry for source_id received from 
        K+K TCP server and write it to KK_Result.data.
        KK_Result.data = None, if no reports available.
        For details see Multi_GetTcpLog in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful, KK_Result.data contains report or
        None, if no report available
        KK_ERR_BUFFER_TOO_SMALL: operation successful, but string in 
        KK_Result.data is truncated to 1024 characters.
        all other codes are error numbers and KK_Result.data contains 
        error message:
        KK_PARAM_ERROR: invalid source_id
        KK_ERR_SERVER_DOWN: K+K TCP server is not online. Connection is closed.
        KK_ERR_BUFFER_OVERFLOW: Data was lost. App does not read fast enough.
        """
        id_ = c_int32(source_id)
        # mutable 1024 bytes buffer needed
        char_array = ctypes.c_char * len(self._buffer)
        # get log report
        retI = c_int32(self._kkdll.Multi_GetTcpLog(id_, 
                        char_array.from_buffer(self._buffer)))
        kkres = KK_Result()
        # convert to string
        kkres.data = bytearray2string(self._buffer)
        if retI.value == 6:
            kkres.result_code = ErrorCode.KK_ERR_BUFFER_TOO_SMALL
        elif retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        elif retI.value == 4:
            kkres.result_code = ErrorCode.KK_ERR_SERVER_DOWN
        elif retI.value == 8:
            kkres.result_code = ErrorCode.KK_ERR_BUFFER_OVERFLOW
        return kkres
    
    def send_TCP_data(self, source_id: int, data: str) -> KK_Result:
        """Sends the string contained in data to the TCP server connected for 
        source_id at library level or log level and delivers response. 
        For details see Multi_TcpAppData in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param data: string to send 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful, response in KK_Result.data
        all other codes are error numbers and KK_Result.data contains 
        error message:
        KK_PARAM_ERROR: invalid source_id
        KK_ERR_SERVER_DOWN: no connection to K+K TCP server
        KK_ERR: error reported by K+K TCP server
        """
        id_ = c_int32(source_id)
        data_ = data.encode('ascii')
        # data needs 1024 byte buffer
        for i in range(len(data_)):
            self._buffer[i] = data_[i]
        # append terminating 0
        self._buffer[len(data_)] = 0
        # mutable variable needed
        char_array = ctypes.c_char * len(self._buffer)
        retI = c_int32(self._kkdll.Multi_TcpAppData(
                id_, char_array.from_buffer(self._buffer)))
        kkres = KK_Result()
        # convert to string
        kkres.data = bytearray2string(self._buffer)
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
        elif retI.value == 4:
            kkres.result_code = ErrorCode.KK_ERR_SERVER_DOWN
        elif retI.value != 1:
            kkres.result_code = ErrorCode.KK_ERR
        return kkres
    
    
    #-------------------------------------------------------------------------
    # Test data
    #-------------------------------------------------------------------------
    
    def start_save_binary_data(self, source_id: int, dbg_id: str) -> KK_Result:
        """Start generating test data for source_id in binary file.
        For details see Multi_StartSaveBinaryData in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param dbg_id: source identifier of binary file, part of file name 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful
        KK_PARAM_ERROR: invalid source_id, no connection for source_id,
            KK_Result.data contains error message
        """
        id_ = c_int32(source_id)
        ascii_id = None
        if dbg_id is not None:
            ascii_id = dbg_id.encode('ascii')
        retI = c_int32(self._kkdll.Multi_StartSaveBinaryData(id_, ascii_id)) 
        kkres = KK_Result()
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter source_id: "+str(source_id)
        elif retI.value == 7:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "No connection for source_id: "+str(source_id)
        return kkres
    
    def stop_save_binary_data(self, source_id: int) -> KK_Result:
        """Stops generating binary test data for source_id, closes
        binary file.
        Binary file is closed too, when current connection is closed.
        For details see Multi_StopSaveBinaryData in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful
        KK_PARAM_ERROR: invalid source_id, no connection for source_id,
            KK_Result.data contains error message
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_StopSaveBinaryData(id_)) 
        kkres = KK_Result()
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter source_id: "+str(source_id)
        elif retI.value == 7:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "No connection for source_id: "+str(source_id)
        return kkres
    
    def start_save_report_data(self, source_id: int, dbg_id: str) -> KK_Result:
        """Start generating test data for source_id in text file.
        For details see Multi_StartSaveReportData in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @param dbg_id: source identifier of binary file, part of file name 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains error message
        """
        id_ = c_int32(source_id)
        ascii_id = None
        if dbg_id is not None:
            ascii_id = dbg_id.encode('ascii')
        retI = c_int32(self._kkdll.Multi_StartSaveReportData(id_, ascii_id)) 
        kkres = KK_Result()
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter source_id: "+str(source_id)
        return kkres
    
    def stop_save_report_data(self, source_id: int) -> KK_Result:
        """Stops generating report test data for source_id, closes
        text file.
        For details see Multi_StopSaveReportData in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains error message
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_StopSaveReportData(id_)) 
        kkres = KK_Result()
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter source_id: "+str(source_id)
        return kkres
    
    
    #-------------------------------------------------------------------------
    # Device specific
    #-------------------------------------------------------------------------
    
    def request_connected_user(self, source_id: int) -> KK_Result:
        """Requests information about connected user from K+K device connected
        to source_id. K+K device responds with a 0x7030 report.
        For details see Multi_RequestConnectedUser in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains error message
        KK_CMD_IGNORED: command rejected
        KK_ERR_NOT_SUPPORTED: feature not supported
        KK_DLL_EXCEPTION: exception error
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_RequestConnectedUser(id_)) 
        kkres = KK_Result()
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter source_id: "+str(source_id)
        elif retI.value == 11:
            kkres.result_code = ErrorCode.KK_CMD_IGNORED
        elif retI.value == 12:
            kkres.data = "Feature not supported"
            kkres.result_code = ErrorCode.KK_ERR_NOT_SUPPORTED
        elif retI.value == 15:
            kkres.result_code = ErrorCode.KK_DLL_EXCEPTION
        return kkres
    
    def reset_device(self, source_id: int) -> KK_Result:
        """Sends reset command to K+K device connected to source_id.
        WARNING: Device reset disturbs all connected users!
        Use for troubleshooting only!
        For details see Multi_ResetDevice in K+K library manual.
        @param source_id: source identifier returned by get_source_id 
        @return see KK_Result.resultCode
        KK_NO_ERR: operation successful, all connections are invalid
        and must be re-opened
        KK_PARAM_ERROR: invalid source_id, KK_Result.data contains error message
        KK_CMD_IGNORED: command rejected
        KK_ERR_NOT_SUPPORTED: feature not supported
        KK_DLL_EXCEPTION: exception error
        """
        id_ = c_int32(source_id)
        retI = c_int32(self._kkdll.Multi_ResetDevice(id_)) 
        kkres = KK_Result()
        if retI.value == 10:
            kkres.result_code = ErrorCode.KK_PARAM_ERROR
            kkres.data = "Invalid parameter source_id: "+str(source_id)
        elif retI.value == 11:
            kkres.result_code = ErrorCode.KK_CMD_IGNORED
        elif retI.value == 12:
            kkres.data = "Feature not supported"
            kkres.result_code = ErrorCode.KK_ERR_NOT_SUPPORTED
        elif retI.value == 15:
            kkres.result_code = ErrorCode.KK_DLL_EXCEPTION
        return kkres

