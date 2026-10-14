K+K Library Python-Support

Version 2.1
Date    2025-06-03
Author  Loryn Brendes, lb@brendes.de

K+K Messtechnik GmbH
https://www.kplusk-messtechnik.de
info@kplusk-messtechnik.de

------------------------------------------------------------------------------
Contains wrapper class NativeLib and demo programs

1. kklib.py - wrapper class K+K Library
Native access to KK-Library for
  Windows
  -------
    64 bit: KK_Library_64.dll
    32 bit: KK_FX80E.dll
  Raspberry Pi OS (ARM)
  ---------------------
    64 bit: libkk_linux_aarch64_cdecl.so
    32 bit: libkk_linux_arm32_cdecl.so
  Linux
  -----
    64 bit: libkk_library_64_cdecl.so
    32 bit: libkk_library_32_cdecl.so

needs KK-Library version 21.1.0 or higher
Please download corresponding KK-Library from K+K Website: https://www.kplusk-messtechnik.de/downloads/


2. logreceiver.py
Demo program, shows use of kklib (access to native K+K library)
Reads log reports from K+K Server, sends TCP appData


3. reportreceiver.py
Demo program, shows use of kklib (access to native K+K library)
Read string reports from K+K Device

4. reportstructreceiver.py
Demo program, shows use of kklib (access to native K+K library)
Read binary reports from K+K Device
