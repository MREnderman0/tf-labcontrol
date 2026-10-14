/******************************************************************************
** PROJECT: KK Library
** FILE:    kk_library.h
**
** CONTACT: info@kplusk-messtechnik.de
**
**
**                     -- ABSTRACT --
**
** C Header file includes function declarations of exported functions from
** Multi Source KK Library version 21.00.00
** KK Library controls and communicates with K+K measuring cards.
**
**
**                     -- HISTORY --
**
**  2019-09-26  lb  new created
**  2020-10-01  lb  updated KK Library version 18.01.10
**  2021-05-28  lb  updated KK Library version 19.00.02
**  2021-11-18  lb  updated KK Library version 19.01.02
**  2021-12-20  lb  updated KK Library version 19.02.00
**  2022-04-08  lb  syntax error Multi_DebugGetFilename
**  2024-04-15  lb  updated KK Library version 20.02.00
**  2024-12-04  lb  updated KK Library version 20.02.06
**                  const added
**  2025-04-10  lb  updated KK Library version 21.00.00
**  2025-06-03  lb  updated KK Library version 21.01.00
**  2025-08-28  lb  updated KK Library version 21.02.00
******************************************************************************/

//*****************************************************************************
// const
//*****************************************************************************

// buffer size                  -----------------------------------------------
#define CKK_DLL_BufferSize          1024

// channel count                -----------------------------------------------
#define CKK_MAX_CHANNELS            24

// function return codes        -----------------------------------------------
#define CKK_DLL_Error               0
#define CKK_DLL_NoError             1

#define CKK_DLL_WriteError          3
#define CKK_DLL_ServerDownError     4
#define CKK_DLL_StreamInterrupted   5

// response string was longer than CKK_DLL_BufferSize
//  -> shortened to CKK_DLL_BufferSize characters
#define CKK_DLL_BufferTooSmall      6

// USB cable was pulled, end of file, network connection disconnected 
#define CKK_DLL_DeviceNotConnected  7

// receive buffer overflow -> data loss
#define CKK_DLL_BufferOverflow      8

// device does not send measurement data -> connection closed
#define CKK_DLL_HardwareFault       9

// invalid source ID
#define CKK_DLL_SourceNotFound      10

// command rejected (no connection, list full, format error, too long)
#define CKK_DLL_CmdIgnored          11

// feature is not supported by current firmware version
#define CKK_DLL_NotSupported        12

// network connection reconnected after interruption 
#define CKK_DLL_Reconnected         13

// function return codes since 20.0.0   ---------------------------------------
// interrupted data connection is running again
#define CKK_DLL_Recovered           14

// function return codes since 20.2.1   ---------------------------------------
// exception occurred, please report to K+K
#define CKK_DLL_EXCEPTION           15


//*****************************************************************************
// report struct
//
// since 21.00.00
//*****************************************************************************

// buffer size                  -----------------------------------------------
#define CKK_REPORT_BUFFER_SIZE      512
#define CKK_REPORT_CONTENT_SIZE     CKK_REPORT_BUFFER_SIZE-1-1-2-4-8 
#define CKK_REPORT_INDEX_DECIMALS   CKK_MAX_CHANNELS*16

// report type                  -----------------------------------------------
#define CRT_NONE                    0
#define CRT_ERROR                   1
#define CRT_MESSAGE                 2
#define CRT_PHASe                   3
#define CRT_DOUBLE                  4
#define CRT_INT32                   5

// type report struct           -----------------------------------------------
typedef struct {
  unsigned char   ReportType;
  unsigned char   ErrorCode;
  unsigned short  Header;
  int             Len;        // Number of valid bytes in content
  int64           DeviceMs;   // ms counter of the device since PowerUp or Re-Sync,
                              // int64 lasts for centuries
  unsigned char   Content[CKK_REPORT_CONTENT_SIZE];
} TKK_ReportStructBase;


//*****************************************************************************
// functions
//*****************************************************************************

// create multi source object   -----------------------------------------------
int CreateMultiSource(void);

// list available interfaces    -----------------------------------------------
int    Multi_EnumerateDevices(char *Names, unsigned char EnumFlags);
char * Multi_GetEnumerateDevicesErrorMsg(void);
int    Multi_GetHostAndIPs(char *HostName, char *IPaddr, char * ErrorMsg);
// hint requires 80 byte buffer only

// path definitions             -----------------------------------------------
char * Multi_GetOutputPath(int ID);
char * Multi_SetOutputPath(int ID, char *path);

// debug protocol               -----------------------------------------------
char * Multi_Debug(int ID, bool DbgOn, char *DbgID);
int    Multi_DebugFlags(int ID, bool ReportLog, bool LowLevelLog);
int    Multi_DebugLogLimit(int ID, unsigned char LogType, unsigned int aSize);
char * Multi_DebugGetFilename(int ID);
//     since 20.02.00
char * Multi_Debug_ErrorOnly(int ID, bool DbgOn, char *DbgID);
char * Multi_DebugStream(int ID, bool DbgOn, char *DbgID, bool errorOnly);

// info queries                 -----------------------------------------------
char * Multi_GetDLLVersion(void);
int    Multi_GetBufferAmount(int ID);
int    Multi_GetTransmitBufferAmount(int ID);
unsigned char Multi_GetUserID(int ID);
bool   Multi_IsFileDevice(int ID);
//     since 18.01.10
int    Multi_GetFirmwareVersion(int ID);
bool   Multi_HasFRAM(int ID);
//     since 19.01.02
bool   Multi_IsSerialDevice(int ID);
//     since 19.03.01
int    Multi_GetDeviceStartState(int ID);

// calibration                  -----------------------------------------------
//     since 18.01.10
int    Multi_SetNSZCalibrationData(int ID, char *Data);

// FHR-Settings                 -----------------------------------------------
//     since 19.01.02
int    Multi_ReadFHRData(int ID);
int    Multi_SetFHRData(int ID, char *Data);

// open and close connection    -----------------------------------------------
int    Multi_OpenConnection(int ID, char *Connection, bool BlockingIO);
void   Multi_CloseConnection(int ID);
int    Multi_SetServerIPforPeer(int ID, char *IP);

// read reports                 -----------------------------------------------
int    Multi_SetDecimalSeparator(int ID, char Separator);
int    Multi_SetNSZ(int ID, int aNSZ);
int    Multi_GetReport(int ID, char *Data);
//     since 21.0.0
int    Multi_GetReportStruct(int ID, TKK_ReportStructBase *Data);

// send commands                -----------------------------------------------
unsigned int Multi_GetPendingCmdsCount(int ID);
int    Multi_SetCommandLimit(int ID, unsigned int Limit);
int    Multi_SendCommand(int ID, char *Command, int Len);
//     since 18.01.10
int    Multi_RemoteLogin(int ID, unsigned int Password, char *err);

// local TCP server             -----------------------------------------------
int    Multi_StartTcpServer(int ID, unsigned short *aPort);
int    Multi_StopTcpServer(int ID);
char * Multi_GetTcpServerError(int ID);
void   Multi_TcpReportLog(int ID, char *Data, int logType);
//     since 21.1.0
void   Multi_TcpReportLogTime(int ID, char *Data, int logType, double fxeTime);

// connection to TCP server at LOG level  -------------------------------------
int    Multi_OpenTcpLog(int ID, char *IpPort, char *Mode);
void   Multi_CloseTcpLog(int ID);
int    Multi_GetTcpLog(int ID, char *Data);
//     since 19.0.2
int    Multi_OpenTcpLogTime(int ID, char *IpPort, char *Mode, char *Format);
//     since 19.2.2
int    Multi_OpenTcpLogType(int ID, char *IpPort, int LogType, char *Format);
//     since 21.1.0
int    Multi_TcpAppData(int ID, char *Data);
int    Multi_OpenTcpLogTypeFxe(int ID, char *IpPort, int LogType, char *Format, char *FormatFxe);

// send data to TCP server      -----------------------------------------------
int    Multi_TcpAppData(int ID, char *Data);

// generate test data           -----------------------------------------------
int    Multi_StartSaveBinaryData(int ID, char *DbgID);
int    Multi_StopSaveBinaryData(int ID);
int    Multi_StartSaveReportData(int ID, char *DbgID);
int    Multi_StopSaveReportData(int ID);

// device specific                  -----------------------------------------------
//     since 20.02.06
int    Multi_RequestConnectedUser(int ID);
int    Multi_ResetDevice(int ID);
