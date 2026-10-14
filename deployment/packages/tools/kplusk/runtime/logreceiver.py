'''
Demo program, shows use of kklib (access to native K+K library)
Reads log reports from K+K Server

TODO:	Configure program sequence and variables

Sets output path to _path.

Uses debug log file depending on _error_log.

Connects to K+K server _server
Opens TCP log _log_type with time format _time_format
    
Receive TCP logs in an endless loop. 
Loop is left, if no more data is received from TCP server or a max. receive count is reached.

Send TCP appData.

Created on 14.01.2019
Changed lb 27.05.2021 open_TCP_log_time
Changed lb 16.12.2021 open_TCP_log_type
Changed lb 05.01.2022 send_TCP_data
Changed lb 09.10.2022 send_TCP_data: deliver response
Changed lb 17.04.2024 config variables added
Changed lb 03.06.2025 TCP log with or without UTC/FXE timestamp

@author: Loryn Brendes, lb@brendes.de
'''

import sys
import time
import platform
import os

from kklib import NativeLib, NativeLibError
from kklib import ErrorCode
from kklib import DebugLogType

print("LogReceiver 2.1 2025-06-03 start")

# TODO config settings:

# set your own value in _path
#
_path = "."+os.sep+"Debug"

# keep all debug files
#_error_only = False
# keep only debug files with error(s)
_error_only = True

# set IP address and port of TCP server
#
_server = "127.0.0.1:48896"

# set log type number to frequency log
_log_type = 1

# set log time format date and time
_time_format_long = "YYYYMMDD HH:NN:SS.ZZZ"

# set log time format time
_time_format_short = "HH:NN:SS.ZZZ"

# Limit report counts: end while-get_report-loop
_empty_string_limit = 50
_limit_receive_counter = 25;

try:
    # load K+K library
    _kknative = NativeLib();
except NativeLibError as exc:
    # stop execution
    sys.exit(str(exc))    
    
# show version
print("K+K library version "+_kknative.get_version())

# get source ID
_source_id = _kknative.get_source_id()

# set output path
kkres = _kknative.set_output_path(_source_id, _path)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_output_path: unexpected Error")
    print("set_output_path: "+kkres.data)
else:
    print("set_output_path: ok")
# check it
kkres = _kknative.get_output_path(_source_id)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("get_output_path: unexpected Error")
print("get_output_path: "+kkres.data)

# DebugLog
# report log only
kkres = _kknative.set_debug_flags(_source_id, True, False)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_debug_flags failed: "+kkres.data)
kkres = _kknative.set_debug_log_limit(_source_id, 
                                      DebugLogType.LOG_CREATE_NEW, 
                                      0)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_debug_log_limit failed: "+kkres.data)
kkres = _kknative.set_debug_log(_source_id, True, None, _error_only)
if kkres.result_code != ErrorCode.KK_NO_ERR:
    print("set_debug_log failed: "+kkres.data)

# test enumerate ips
l = []
kkres = _kknative.get_host_and_IPs(l)
if kkres.result_code == ErrorCode.KK_ERR:
    print("get_host_and_IPs failed: "+kkres.data)
first_ = True
for s in l:
    if first_:
        print("host: "+s)
        first_ = False
    else:
        print("IP: "+s)

# open TCP log
#
print("connect to server: "+_server)
# 
# log without timestamps
#kkres = _kknative.open_TCP_log_type(_source_id, _server, _log_type, None, None)
# log with UTC timestamp
#kkres = _kknative.open_TCP_log_type(_source_id, _server, _log_type, _time_format_long)
# log with FXE timestamp
#kkres = _kknative.open_TCP_log_type(_source_id, _server, _log_type, None, _time_format_long)
# log with UTC timestamp and FXE timestamp (FXE timestamp without date)
kkres = _kknative.open_TCP_log_type(_source_id, _server, _log_type, _time_format_long, _time_format_short)

if kkres.result_code != ErrorCode.KK_NO_ERR:
    # stop execution
    sys.exit("open_TCP_log failed: "+kkres.data)

# receive TCP logs
end = False
emptyStringCounter = 0
receivedCounter = 0
while (not end):
    kkres = _kknative.get_TCP_log(_source_id)
    # examine result code
    if kkres.result_code == ErrorCode.KK_ERR_BUFFER_OVERFLOW:
        print("get_Tcp_log reports overflow error")
    elif kkres.result_code == ErrorCode.KK_ERR_SERVER_DOWN:
        print("get_Tcp_log reports no connection to TCP server")
        end = True
    elif ((kkres.result_code != ErrorCode.KK_NO_ERR) and 
          (kkres.result_code != ErrorCode.KK_ERR_BUFFER_TOO_SMALL)):
        print("get_Tcp_log failed: "+kkres.data)
        end = True
    elif kkres.data is None:
        # no data
        emptyStringCounter += 1
        if emptyStringCounter > _empty_string_limit:
            end = True
            print("no more data -> stop")
        else:
            time.sleep(0.1) # 100ms
    else :
        print(kkres.data)
        # reset counter
        emptyStringCounter = 0
        receivedCounter += 1
        if receivedCounter % 10 == 0:
            print("send data to TCP server")
            tcpData = "TCP Data after "+str(receivedCounter)+" received logs"
            kkres = _kknative.send_TCP_data(_source_id, tcpData)
            if kkres.result_code == ErrorCode.KK_NO_ERR:
                print("send data to TCP server: "+kkres.data)
            else:
                print("send data to TCP server failed: "+kkres.data)
        elif receivedCounter > _limit_receive_counter:
            print("more than "+str(_limit_receive_counter)+" logs received -> stop")
            end = True  
          
            
#close TCP log
_kknative.close_TCP_log(_source_id)


print("LogReceiver END")