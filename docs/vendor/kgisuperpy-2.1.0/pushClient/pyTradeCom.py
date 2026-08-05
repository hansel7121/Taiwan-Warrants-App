#----------------------------------------------------------------------------
import ctypes, platform, os, sys
#----------------------------------------------------------------------------
if platform.system() == 'Windows':
    print('----- TradeCom Python API detect : Windows platform -----')
    if (sys.maxsize > 2**32) == True:
        print('Load['+os.path.dirname(__file__) + '/tradecom_windows_bridge64.dll'+']')
        libTradeCom = ctypes.cdll.LoadLibrary( os.path.dirname(__file__) +  '/tradecom_windows_bridge64.dll')
    else:
        print('Load['+os.path.dirname(__file__) + '/tradecom_windows_bridge.dll'+']')
        libTradeCom = ctypes.cdll.LoadLibrary( os.path.dirname(__file__) +  '/tradecom_windows_bridge.dll')
elif platform.system() == 'Linux':
    print('----- TradeCom Python API detect : Linux platform -----')
    print('Load['+os.path.dirname(__file__) + '/tradecom_linux_bridge.so'+']')
    libTradeCom = ctypes.cdll.LoadLibrary(os.path.dirname(__file__) + '/tradecom_linux_bridge.so')

#----------------------------------------------------------------------------
# Declare the callback type
StatusChangedCallback        = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)
ReportOrQueryCallback        = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p)

# SetDebugMode
libTradeCom.pyTradeCom_SetDebugMode.argtypes = [ctypes.c_int]
libTradeCom.pyTradeCom_SetDebugMode.restype = ctypes.c_void_p
# New
libTradeCom.pyTradeCom_New.argtypes = [ctypes.c_char_p]
libTradeCom.pyTradeCom_New.restype = ctypes.c_void_p
# Delete
libTradeCom.pyTradeCom_Delete.argtypes = [ctypes.c_void_p]
libTradeCom.pyTradeCom_Delete.restype = None
# SetStrategyName
libTradeCom.pyTradeCom_SetStrategyName.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
libTradeCom.pyTradeCom_SetStrategyName.restype = None
# KeepPushClientData
libTradeCom.pyTradeCom_KeepPushClientData.argtypes = [ctypes.c_void_p, ctypes.c_int]
libTradeCom.pyTradeCom_KeepPushClientData.restype = None
# GetOrderErrMsg
libTradeCom.pyTradeCom_GetOrderErrMsg.argtypes = [ctypes.c_void_p, ctypes.c_int]
libTradeCom.pyTradeCom_GetOrderErrMsg.restype = ctypes.c_char_p
# SetCA_PFX
libTradeCom.pyTradeCom_SetCA_PFX.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
libTradeCom.pyTradeCom_SetCA_PFX.restype = None
# SetCA_PW
libTradeCom.pyTradeCom_SetCA_PW.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
libTradeCom.pyTradeCom_SetCA_PW.restype = None
# GetSenderPtr
libTradeCom.pyTradeCom_GetSenderPtr.argtypes = [ctypes.c_void_p]
libTradeCom.pyTradeCom_GetSenderPtr.restype = ctypes.c_void_p
# SetCallbackFunction
libTradeCom.pyTradeCom_SetCallbackFunction.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
libTradeCom.pyTradeCom_SetCallbackFunction.restype = None
# Connect
libTradeCom.pyTradeCom_Connect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ushort, ctypes.c_char_p]
libTradeCom.pyTradeCom_Connect.restype = None
# Login
libTradeCom.pyTradeCom_Login.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
libTradeCom.pyTradeCom_Login.restype = None
# Logout
libTradeCom.pyTradeCom_Logout.argtypes = [ctypes.c_void_p]
libTradeCom.pyTradeCom_Logout.restype = None
# GetAccounts
libTradeCom.pyTradeCom_GetAccounts.argtypes = [ctypes.c_void_p]
libTradeCom.pyTradeCom_GetAccounts.restype = ctypes.c_char_p
# RetriveRequestID
libTradeCom.pyTradeCom_RetriveRequestID.argtypes = [ctypes.c_void_p]
libTradeCom.pyTradeCom_RetriveRequestID.restype = ctypes.c_ulonglong
# SecurityOrder
libTradeCom.pyTradeCom_SecurityOrder.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_ulonglong,
                                                 ctypes.c_int,
                                                 ctypes.c_int,
                                                 ctypes.c_int,
                                                 ctypes.c_char_p,
                                                 ctypes.c_char_p,
                                                 ctypes.c_char_p,
                                                 ctypes.c_char_p,
                                                 ctypes.c_int,
                                                 ctypes.c_char_p,
                                                 ctypes.c_int,
                                                 ctypes.c_char_p,
                                                 ctypes.c_char_p,
                                                 ctypes.c_char_p,
                                                 ctypes.c_int]    
libTradeCom.pyTradeCom_SecurityOrder.restype = ctypes.c_ulonglong
# SecuritySubOrder
libTradeCom.pyTradeCom_SecuritySubOrder.argtypes = [ctypes.c_void_p,
                                                    ctypes.c_ulonglong,
                                                    ctypes.c_int,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_int,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_int,
                                                    ctypes.c_char_p,
                                                    ctypes.c_int,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_int]    
libTradeCom.pyTradeCom_SecuritySubOrder.restype = ctypes.c_ulonglong
# Order
libTradeCom.pyTradeCom_Order.argtypes = [ctypes.c_void_p,
                                         ctypes.c_int,
                                         ctypes.c_int,
                                         ctypes.c_ulonglong,
                                         ctypes.c_char_p,
                                         ctypes.c_char_p,
                                         ctypes.c_char_p,
                                         ctypes.c_char_p,
                                         ctypes.c_char_p,
                                         ctypes.c_int,
                                         ctypes.c_char_p,
                                         ctypes.c_int,
                                         ctypes.c_int,
                                         ctypes.c_int,
                                         ctypes.c_int,
                                         ctypes.c_char_p,
                                         ctypes.c_char_p,
                                         ctypes.c_char_p]    
libTradeCom.pyTradeCom_Order.restype = ctypes.c_ulonglong
# FOrderSingle
libTradeCom.pyTradeCom_FOrderSingle.argtypes = [ctypes.c_void_p,
                                                ctypes.c_int,
                                                ctypes.c_int,
                                                ctypes.c_ulonglong,
                                                ctypes.c_int,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_int,
                                                ctypes.c_char_p,
                                                ctypes.c_int,
                                                ctypes.c_int,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_int,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p,
                                                ctypes.c_char_p]    
libTradeCom.pyTradeCom_FOrderSingle.restype = ctypes.c_ulonglong
# FOrderMulti
libTradeCom.pyTradeCom_FOrderMulti.argtypes = [ctypes.c_void_p,
                                               ctypes.c_int,
                                               ctypes.c_int,
                                               ctypes.c_ulonglong,
                                               ctypes.c_int,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_int,
                                               ctypes.c_char_p,
                                               ctypes.c_int,
                                               ctypes.c_int,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_int,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p,
                                               ctypes.c_char_p]    
libTradeCom.pyTradeCom_FOrderMulti.restype = ctypes.c_ulonglong
# ReceiveSReport
libTradeCom.pyTradeCom_ReceiveSReport.argtypes = [ctypes.c_void_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_char_p]
libTradeCom.pyTradeCom_ReceiveSReport.restype = None
# ReceiveFReport
libTradeCom.pyTradeCom_ReceiveFReport.argtypes = [ctypes.c_void_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_bool]
libTradeCom.pyTradeCom_ReceiveFReport.restype = None
# ReceiveReport
libTradeCom.pyTradeCom_ReceiveReport.argtypes = [ctypes.c_void_p,
                                                     ctypes.c_char_p,
                                                     ctypes.c_char_p]
libTradeCom.pyTradeCom_ReceiveReport.restype = None
# ReceivePositionSum 
libTradeCom.pyTradeCom_RetrivePositionSum.argtypes = [ctypes.c_void_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrivePositionSum.restype = None
# RetrivePositionDetail
libTradeCom.pyTradeCom_RetrivePositionDetail.argtypes = [ctypes.c_void_p,
                                                             ctypes.c_char_p,
                                                             ctypes.c_char_p,
                                                             ctypes.c_char_p,
                                                             ctypes.c_char_p,
                                                             ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrivePositionDetail.restype = None
# RetriveFMargin
libTradeCom.pyTradeCom_RetriveFMargin.argtypes = [ctypes.c_void_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_char_p,
                                                      ctypes.c_char_p]
libTradeCom.pyTradeCom_RetriveFMargin.restype = None
# RetriveFMargin_EX
libTradeCom.pyTradeCom_RetriveFMargin_EX.argtypes = [ctypes.c_void_p,
                                                         ctypes.c_char_p,
                                                         ctypes.c_char_p,
                                                         ctypes.c_char_p,
                                                         ctypes.c_char_p,
                                                         ctypes.c_char_p]
libTradeCom.pyTradeCom_RetriveFMargin_EX.restype = None
# RetriveCOVER
libTradeCom.pyTradeCom_RetriveCOVER.argtypes = [ctypes.c_void_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p,
                                                    ctypes.c_char_p]
libTradeCom.pyTradeCom_RetriveCOVER.restype = ctypes.c_long
# RetriveCOVERDetail
libTradeCom.pyTradeCom_RetriveCOVERDetail.argtypes = [ctypes.c_void_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_char_p,
                                                          ctypes.c_ulonglong,
                                                          ctypes.c_char_p]
libTradeCom.pyTradeCom_RetriveCOVERDetail.restype = ctypes.c_long
# RetrieveWsSettleAmtTrial
libTradeCom.pyTradeCom_RetrieveWsSettleAmtTrial.argtypes = [ctypes.c_void_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveWsSettleAmtTrial.restype = ctypes.c_long
# RetrieveWsSettleAmtDetail
libTradeCom.pyTradeCom_RetrieveWsSettleAmtDetail.argtypes = [ctypes.c_void_p,
                                                                 ctypes.c_char_p,
                                                                 ctypes.c_char_p,
                                                                 ctypes.c_char_p,
                                                                 ctypes.c_char_p,
                                                                 ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveWsSettleAmtDetail.restype = ctypes.c_long
# RetrieveWsSettleAmt
libTradeCom.pyTradeCom_RetrieveWsSettleAmt.argtypes = [ctypes.c_void_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveWsSettleAmt.restype = ctypes.c_long
# RetrieveWsInventory
libTradeCom.pyTradeCom_RetrieveWsInventory.argtypes = [ctypes.c_void_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveWsInventory.restype = ctypes.c_long
# RetrieveWsInventorySum
libTradeCom.pyTradeCom_RetrieveWsInventorySum.argtypes = [ctypes.c_void_p,
                                                              ctypes.c_char_p,
                                                              ctypes.c_char_p,
                                                              ctypes.c_char_p,
                                                              ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveWsInventorySum.restype = ctypes.c_long
# RetrieveBalanceStatement
libTradeCom.pyTradeCom_RetrieveWsBalanceStatement.argtypes = [ctypes.c_void_p,
                                                                  ctypes.c_char_p,
                                                                  ctypes.c_char_p,
                                                                  ctypes.c_char_p,
                                                                  ctypes.c_char_p,
                                                                  ctypes.c_char_p,
                                                                  ctypes.c_char_p,
                                                                  ctypes.c_char_p,
                                                                  ctypes.c_int]
libTradeCom.pyTradeCom_RetrieveWsBalanceStatement.restype = ctypes.c_long
# RetrieveWsRealizePL
libTradeCom.pyTradeCom_RetrieveWsRealizePL.argtypes = [ctypes.c_void_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_char_p,
                                                           ctypes.c_int]
libTradeCom.pyTradeCom_RetrieveWsRealizePL.restype = ctypes.c_long
# RetrieveRcOrderReport
libTradeCom.pyTradeCom_RetrieveRcOrderReport.argtypes = [ctypes.c_void_p,
                                                             ctypes.c_char_p,
                                                             ctypes.c_char_p,
                                                             ctypes.c_int]
libTradeCom.pyTradeCom_RetrieveRcOrderReport.restype = ctypes.c_long
# RetrieveRcMatchReportSum
libTradeCom.pyTradeCom_RetrieveRcMatchReportSum.argtypes = [ctypes.c_void_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_int]
libTradeCom.pyTradeCom_RetrieveRcMatchReportSum.restype = ctypes.c_long
# RetrieveRcMatchReportDetail
libTradeCom.pyTradeCom_RetrieveRcMatchReportDetail.argtypes = [ctypes.c_void_p,
                                                                   ctypes.c_char_p,
                                                                   ctypes.c_char_p,
                                                                   ctypes.c_int]
libTradeCom.pyTradeCom_RetrieveRcMatchReportDetail.restype = ctypes.c_long
# RetrieveRcPositionDetailReport
libTradeCom.pyTradeCom_RetrieveRcPositionDetailReport.argtypes = [ctypes.c_void_p,
                                                                      ctypes.c_char_p,
                                                                      ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveRcPositionDetailReport.restype = ctypes.c_long
# RetrieveRcCurrencyReport
libTradeCom.pyTradeCom_RetrieveRcCurrencyReport.argtypes = [ctypes.c_void_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveRcCurrencyReport.restype = ctypes.c_long
# RetrieveRcStockPositionReport
libTradeCom.pyTradeCom_RetrieveRcStockPositionReport.argtypes = [ctypes.c_void_p,
                                                                     ctypes.c_char_p,
                                                                     ctypes.c_char_p,
                                                                     ctypes.c_int,
                                                                     ctypes.c_int]
libTradeCom.pyTradeCom_RetrieveRcStockPositionReport.restype = ctypes.c_long
# RetrieveRcDeliveryReport
libTradeCom.pyTradeCom_RetrieveRcDeliveryReport.argtypes = [ctypes.c_void_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p,
                                                                ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveRcDeliveryReport.restype = ctypes.c_long
# RetrieveTSECreditInfo
libTradeCom.pyTradeCom_RetrieveTSECreditInfo.argtypes = [ctypes.c_void_p,
                                                            ctypes.c_char_p,
                                                            ctypes.c_int,
                                                            ctypes.c_char_p]
libTradeCom.pyTradeCom_RetrieveTSECreditInfo.restype = ctypes.c_long
# RetriveSecProduct
libTradeCom.pyTradeCom_RetriveSecProduct.argtypes = [ctypes.c_void_p]
libTradeCom.pyTradeCom_RetriveSecProduct.restype = None
# RetrieveFProduct
libTradeCom.pyTradeCom_RetrieveFProduct.argtypes = [ctypes.c_void_p]
libTradeCom.pyTradeCom_RetrieveFProduct.restype = None

gStatusChangedCB = {}
gOrderPendingCB = {}
gOrderReportCB = {}
gExecReportCB = {}
gQueryResultCB = {}

def StatusChangedCB(sender, status):
    if sender in gStatusChangedCB:
        gStatusChangedCB[sender](status)

def OrderPendingCB(sender, type, response):
    if sender in gOrderPendingCB:
        gOrderPendingCB[sender](type, response.decode('utf-8'))

def OrderReportCB(sender, type, response):
    if sender in gOrderReportCB:
        gOrderReportCB[sender](type, response.decode('utf-8'))

def ExecReportCB(sender, type, response):
    if sender in gExecReportCB:
        gExecReportCB[sender](type, response.decode('utf-8'))

def QueryResultCB(sender, type, response):
    if sender in gQueryResultCB:
        gQueryResultCB[sender](type, response.decode('utf-8')) 

gFuncStatusChangedCallback = StatusChangedCallback(StatusChangedCB)
gFuncOrderPendingCallback = ReportOrQueryCallback(OrderPendingCB)
gFuncOrderReportCallback = ReportOrQueryCallback(OrderReportCB)
gFuncExecReportCallback = ReportOrQueryCallback(ExecReportCB)
gFuncQueryResultCallback = ReportOrQueryCallback(QueryResultCB)
   
#----------------------------------------------------------------------------
#
# class TradeComAPI
#
#----------------------------------------------------------------------------
class TradeComAPI():
    #------------------------------------------------------------------------
    # Functions
    #------------------------------------------------------------------------
    def __init__(self, iid = ''):
        libdir = os.path.dirname(__file__) 
        CurDir = os.path.abspath(os.getcwd())
        os.chdir( libdir )        
        
        if iid == None:
            self.Obj = libTradeCom.pyTradeCom_New(''.encode())
        else:
            self.Obj = libTradeCom.pyTradeCom_New(iid.encode())
            
        os.chdir( CurDir )
                
        print("Package install directory: {0}".format(libdir))
        print("Current working directory: {0}".format(CurDir))        
    
        sender_id = libTradeCom.pyTradeCom_GetSenderPtr(self.Obj)
        
        global gStatusChangedCB, gOrderPendingCB, gOrderReportCB, gExecReportCB, gQueryResultCB
        global gFuncStatusChangedCallback, gFuncOrderPendingCallback, gFuncOrderReportCallback, gFuncExecReportCallback, gFuncQueryResultCallback
        
        gStatusChangedCB[sender_id] = self.OnStatusChanged
        gOrderPendingCB[sender_id] = self.OnOrderPending
        gOrderReportCB[sender_id] = self.OnOrderReport
        gExecReportCB[sender_id] = self.OnExecReport
        gQueryResultCB[sender_id] = self.OnQueryResult
        
        libTradeCom.pyTradeCom_SetCallbackFunction(self.Obj, 
                                                   gFuncStatusChangedCallback,
                                                   gFuncOrderPendingCallback,
                                                   gFuncOrderReportCallback,
                                                   gFuncExecReportCallback,
                                                   gFuncQueryResultCallback)
                                                   
    ##設定憑證-PFX路徑
    # @param pfx	憑證存放路徑 (含憑證檔名)。
    def SetCA_PFX(self, pfx):
        libTradeCom.pyTradeCom_SetCA_PFX(self.Obj, pfx.encode())
    
    ##設定憑證-PW
    # @param pw	憑證存放路徑 (含憑證檔名)。
    def SetCA_PW(self, pw):
        libTradeCom.pyTradeCom_SetCA_PW(self.Obj, pw.encode())
    
    ## 設定策略名稱 (名稱最長為 16 個字，超過的部分會被捨棄)
    def SetStrategyName(self, exename):
        libTradeCom.pyTradeCom_SetStrategyName(self.Obj, exename.encode())
    
    ## 是否要保留從 PushClient 收到的原始資料
    # @param value  0:不保留  非 0:保留
    #  若 constructor 有指定 iid 則 PushClient 收到的資料會以檔名 [format_id]-[iid].bin 記錄下來
    #  若 constructor 未指定 iid 則 PushClient 收到的資料以檔名 [format_id].bin 記錄下來    
    def KeepPushClientData(self, value):
        libTradeCom.pyTradeCom_KeepPushClientData(self.Obj, value)    
        
    ##取得錯誤代碼所代表的錯誤訊息
    def GetOrderErrMsg(self, code):
        utf8_result = libTradeCom.pyTradeCom_GetOrderErrMsg(self.Obj, code)
        return utf8_result.decode('utf-8')

    ## 設定為除錯模式
    # 在除錯模式下 API 會將較詳細的除錯資訊寫到 pyTradeCom_DEBUG-DD.log 中。DD為日期。
    def EnableDebugMode(self):
        libTradeCom.pyTradeCom_SetDebugMode(1)        
    
    ## 解除除錯模式
    def DisableDebugMode(self):
        libTradeCom.pyTradeCom_SetDebugMode(0)       
    
    ##連線
    # @param Host	    指定連線的 Push Server IP 或 host name
    # @param Port	    指定連線的 Push Server port
    # @param AppName	程式名稱，DMA用戶請最後加上 “_DMA” Ex: API_DMA
    def Connect(self, Host, Port, AppName):
        libTradeCom.pyTradeCom_Connect(self.Obj, Host.encode(), Port, AppName.encode());
    
    ##登入
    # @param ID	登入ID。
    # @param Password	密碼。
    def Login(self, ID, Password):
        libTradeCom.pyTradeCom_Login(self.Obj, ID.encode(), Password.encode());    
    
    ##登出
    def Logout(self):
        libTradeCom.pyTradeCom_Logout(self.Obj);  
    
    ##取得可用的下單帳號列表
    def GetAccounts(self):
        utf8_result = libTradeCom.pyTradeCom_GetAccounts(self.Obj)
        return utf8_result.decode('utf-8')
    
    ##取得下單所需的 RequestID 
    # @return RequestID, 型態為 number
    def RetriveRequestID(self):
        return libTradeCom.pyTradeCom_RetriveRequestID(self.Obj);
    
    ##證劵下單
    # @param RequestId	型態:number  Client下單序號(請注意:此序號須經由 RetriveRequestID 取得，且一單一號，不可重複)
    # @param ordtype	型態:number 下單類別  0:新單 1:刪單 2:改量 3:改價
    # @param ordlot		型態:number 委託別  0:普通 1:盤後零股 2:定價 3:鉅額 4:盤中零股
    # @param ordclass,	型態:number 委託種類 0:現股 3:自資 4:自券 5:一般策略性借券賣出(盤下限制) 6:策略性交易借券賣出(盤下限制豁免) 7:當沖融資 8:當沖融券 9:現先賣
    # @param BrokerId	型態:text   分公司
    # @param Account,	型態:text   帳號
    # @param stockid	型態:text   股票代號
    # @param bs		    型態:text   買賣別  'B':買 'S':賣
    # @param qty	   	型態:number 張數 (零股時為股數？)
    # @param Price		型態:text   價格
    # @param PF		    型態:number 價格旗標  0:限價 1:跌停 2:市價 5:平盤 9:漲停
    # @param SubAccount	型態:text   子帳(非必要輸入欄位) (最多2碼)
    # @param AgentId	型態:text   營業員代號(非必要輸入欄位)
    # @param OrderNo	型態:text   原委託書編號(刪改單使用)
    # @param TIF		型態:number 委託條件(ROD,IOC,FOK) 0:ROD 1:IOC 2:FOK 
    def SecurityOrder(self,
                      RequestId,
                      ordtype,
                      ordlot,
                      ordclass,
                      BrokerId,
                      Account,
                      stockid,
                      bs,
                      qty,
                      Price,
                      PF,
                      subAccount,
                      AgentID,
                      OrderNo,
                      TIF):
        return libTradeCom.pyTradeCom_SecurityOrder(self.Obj,
                                                    RequestId,
                                                    ordtype,
                                                    ordlot,
                                                    ordclass,
                                                    BrokerId.encode(),
                                                    Account.encode(),
                                                    stockid.encode(),
                                                    bs.encode(),
                                                    qty,
                                                    Price.encode(),
                                                    PF,
                                                    subAccount.encode(),
                                                    AgentID.encode(),
                                                    OrderNo.encode(),
                                                    TIF)


    ##複委託下單
    # @param RequestId	型態:number  Client下單序號(請注意:此序號須經由 RetriveRequestID 取得，且一單一號，不可重複)
    # @param ordtype 	型態:number 下單類別 0:新單 1:刪單 2:改量 3:改價
    # @param BrokerId	型態:text   分公司
    # @param Account	型態:text   帳號
    # @param smarket 	型態:number 市場別  0:港股 1:美股 2:滬股通 3:日股
    # @param stockid	型態:number 股票代號
    # @param bs		    型態:text   買賣別 'B':買  S':賣
    # @param qty		型態:number 股數
    # @param Price		型態:text   價格
    # @param curr		型態:number 交割幣別   0:台幣 1:外幣
    # @param OrderNo	型態:text   委託書號(刪改單使用)
    # @param CNT		型態:text   電子單號(刪改單使用)
    # @param afterqty	型態:number 改量後股數(刪改單使用)
    def SecuritySubOrder(self,
                         RequestId,
                         ordtype,
                         BrokerId,
                         Account,
                         smarket,
                         stockid,
                         bs,
                         qty,
                         Price,
                         curr,
                         OrderNo,
                         CNT,
                         afterqty):
        return libTradeCom.pyTradeCom_SecuritySubOrder(self.Obj,
                                                       RequestId,
                                                       ordtype,
                                                       BrokerId.encode(),
                                                       Account.encode(),
                                                       smarket,
                                                       stockid.encode(),
                                                       bs.encode(),
                                                       qty,
                                                       Price.encode(),
                                                       curr,
                                                       OrderNo.encode(),
                                                       CNT.encode(),
                                                       afterqty)


    ##內期下單
    # @param orderType	型態:number  下單類別 0:新單 1:刪單 2:改量 3:改價 4:改單
    # @param MarketFlag	型態:number  期/選 0:期貨 1:選擇權
    # @param RequestId	型態:number  Client下單序號，請使用 RetriveRequestID 取得
    # @param BrokerId	型態:text   分公司
    # @param Account	型態:text   帳號
    # @param SubAccount	型態:text   子帳號 (非必要輸入欄位)
    # @param Symbol	    型態:number 商品代碼( 20碼); 須大(等)於5碼 & 小於20碼。必須為文數字
    # @param BS	        型態:number 買賣別 'B':買  S':賣
    # @param PriceFlag	型態:number 價格別  0:指定價格(限價) 1:市價 2:停損市價 3:停損限價 4:一定範圍市價單
    # @param Price	    型態:text   價格，非市價單不可為 0 
    # @param TIF	    型態:number 0:ROD 1:IOC 2:FOK 
    # @param QTY	    型態:number 口數
    # @param PF	        型態:number 新/平/當沖 0:新倉 1:平倉 2:當沖 3:自動
    # @param officeFlag	型態:number 前台 (保留暫不使用，請代入0)
    # @param WebID	    型態:text   主機別(3碼)   (刪改單時使用)
    # @param CNT	    型態:text   電子單號(8碼) (刪改單時使用)
    # @param OrderNo	型態:text   委託書號(5碼) (刪改單時使用)
    def Order(self,
              orderType,
              marketFlag,
              RequestId,
              BrokerId,
              Account,
              SubAccount,
              Symbol,
              BS,
              priceFlag,
              Price,
              TIF,
              Qty,
              PF,
              officeFlag,
              WebID,
              CNT,
              OrderNo):
        return libTradeCom.pyTradeCom_Order(self.Obj,
                                            orderType,
                                            marketFlag,
                                            RequestId,
                                            BrokerId.encode(),
                                            Account.encode(),
                                            SubAccount.encode(),
                                            Symbol.encode(),
                                            BS.encode(),
                                            priceFlag,
                                            Price.encode(),
                                            TIF,
                                            Qty,
                                            PF,
                                            officeFlag,
                                            WebID.encode(),
                                            CNT.encode(),
                                            OrderNo.encode())

    ##外期單式下單
    # @param orderKind	  型態:number 0:風控回報400,可平台刪 1:不回報400,不可交叉查詢
    # @param orderType	  型態:number 0:新單 1:刪單 2:改量 3:改價 4:改單
    # @param RequestId	  型態:number Client下單序號 請使用 RetriveRequestID() 取得
    # @param MarketFlag	  型態:number 期/選 0:期貨 1:選擇權
    # @param BrokerId	  型態:text   分公司
    # @param Account	  型態:text   帳號
    # @param AE	          型態:text   營業員代號(子帳)
    # @param EXCHANGE	  型態:text   交易所
    # @param Symbol	      型態:text   商品代碼，必須大(等)於2碼 & 小於30碼。必須為文數字
    # @param ComYM	      型態:text   商品年月
    # @param StrikePrice  型態:text   履約價, 格式：固定6位小數、左右補0
    # @param CP	          型態:text   買賣權, 'C':買權 'P':賣權 'F':期貨
    # @param BS	          型態:text   買賣別, 'B':買進 'S':賣出
    # @param PriceFlag	  型態:number 價格別  0:指定價格(限價) 1:市價 2:停損市價 3:停損限價 4:一定範圍市價單
    # @param DayTrade	  型態:text   當日沖銷 'Y':當沖 'N':不當沖，若PF=[當沖] DayTrade=N 依舊視為當沖單
    # @param PF	          型態:number 新/平/當沖 0:新倉 1:平倉 2:當沖 3:自動 註:若PF=[當沖],即使 DayTrade=N 依舊視為當沖單
    # @param TIF	      型態:number 0:ROD 1:IOC 2:FOK 
    # @param OrderPrice	  型態:text   委託價格
    # @param StopPrice	  型態:text   停損價格
    # @param OrderQty	  型態:number 委託口數
    # @param Idno	      型態:text   身份證號(可不輸入)
    # @param FCM	      型態:text   上手券商         (刪改單時使用)
    # @param FFUT_ACCOUNT 型態:text   原委託上手帳號   (刪改單時使用)
    # @param SOURCE	      型態:text   原委託來源別，代入空值 
    # @param CNT	      型態:text   原委託網路單號   (刪改單時使用)
    # @param Ordno	      型態:text   上手委託書號     (刪改單時使用)	
    # @param TradeDate	  型態:text   交易日期         (刪改單時使用)	
    # @param WEBID	      型態:text   原委託平台       (刪改單時使用)	
    # @param Keyin	      型態:text   交易員
    def FOrderSingle(self,
                     OrderKind,
                     orderType,
                     RequestId,
                     marketFlag,
                     BrokerID,
                     Account,
                     AE,
                     EXCHANGE,
                     Symbol,
                     ComYM,
                     StrikePrice,
                     CP,
                     BS,
                     PriceFlag,
                     DayTrade,
                     PF,
                     TIF,
                     OrderPrice,
                     StopPrice,
                     OrderQty,
                     Idno,
                     FCM,
                     FFUT_ACCOUNT,
                     SOURCE,
                     CNT,
                     ordno,
                     TradeDate,
                     WEBID,
                     KeyIn):
        return libTradeCom.pyTradeCom_FOrderSingle(self.Obj,
                                                   OrderKind,
                                                   orderType,
                                                   RequestId,
                                                   marketFlag,
                                                   BrokerID.encode(),
                                                   Account.encode(),
                                                   AE.encode(),
                                                   EXCHANGE.encode(),
                                                   Symbol.encode(),
                                                   ComYM.encode(),
                                                   StrikePrice.encode(),
                                                   CP.encode(),
                                                   BS.encode(),
                                                   PriceFlag,
                                                   DayTrade.encode(),
                                                   PF,
                                                   TIF,
                                                   OrderPrice.encode(),
                                                   StopPrice.encode(),
                                                   OrderQty,
                                                   Idno.encode(),
                                                   FCM.encode(),
                                                   FFUT_ACCOUNT.encode(),
                                                   SOURCE.encode(),
                                                   CNT.encode(),
                                                   ordno.encode(),
                                                   TradeDate.encode(),
                                                   WEBID.encode(),
                                                   KeyIn.encode());

    ##外期複式下單
    # @param orderKind	  型態:number 0:風控回報400,可平台刪 1:不回報400,不可交叉查詢
    # @param orderType	  型態:number 0:新單 1:刪單 2:改量 3:改價 4:改單
    # @param RequestId	  型態:number Client下單序號 請使用 RetriveRequestID() 取得
    # @param MarketFlag	  型態:number 期/選 0:期貨 1:選擇權
    # @param BrokerId	  型態:text   分公司
    # @param Account	  型態:text   帳號
    # @param AE	          型態:text   營業員代號(子帳)
    # @param EXCHANGE	  型態:text   交易所
    # @param Symbol	      型態:text   商品代碼，必須大(等)於2碼 & 小於30碼。必須為文數字
    # @param ComYM	      型態:text   商品年月
    # @param StrikePrice  型態:text   履約價, 格式：固定6位小數、左右補0
    # @param CP	          型態:text   買賣權, 'C':買權 'P':賣權 'F':期貨
    # @param BS	          型態:text   買賣別, 'B':買進 'S':賣出
    # @param PriceFlag	  型態:number 價格別  0:指定價格(限價) 1:市價 2:停損市價 3:停損限價 4:一定範圍市價單
    # @param DayTrade	  型態:text   當日沖銷 'Y':當沖 'N':不當沖，若PF=[當沖] DayTrade=N 依舊視為當沖單
    # @param PF	          型態:number 新/平/當沖 0:新倉 1:平倉 2:當沖 3:自動 註:若PF=[當沖],即使 DayTrade=N 依舊視為當沖單
    # @param TIF	      型態:number 0:ROD 1:IOC 2:FOK 
    # @param OrderPrice	  型態:text   委託價格
    # @param StopPrice	  型態:text   停損價格
    # @param OrderQty	  型態:number 委託口數
    # @param Idno	      型態:text   身份證號(可不輸入)
    # @param FCM	      型態:text   上手券商         (刪改單時使用)
    # @param FFUT_ACCOUNT 型態:text   原委託上手帳號   (刪改單時使用)
    # @param SOURCE	      型態:text   原委託來源別，代入空值 
    # @param CNT	      型態:text   原委託網路單號   (刪改單時使用)
    # @param Ordno	      型態:text   上手委託書號     (刪改單時使用)	
    # @param TradeDate	  型態:text   交易日期         (刪改單時使用)	
    # @param WEBID	      型態:text   原委託平台       (刪改單時使用)	
    # @param Keyin	      型態:text   交易員
    # @param MLEG	      型態:text   是否為複式單 'Y':是 'N':否
    # @param BS2	      型態:text   買賣別2, 'B':買 'S':賣
    # @param Symbol2	  型態:text   商品代碼2
    # @param ComYM2		  型態:text   商品年月2
    # @param StrikePrice2 型態:text   固定6位小數、左右補0
    # @param CP2		  型態:text   買賣權, 'C':買權 'P':賣權, 'F':期貨
    def FOrderMulti(self, 
                    orderKind,
                    orderType,
                    RequestId,
                    marketFlag,
                    BrokerID,
                    Account,
                    AE,
                    EXCHANGE,
                    Symbol,
                    ComYM,
                    StrikePrice,
                    CP,
                    BS,
                    PriceFlag,
                    DayTrade,
                    PF,
                    TIF,
                    OrderPrice,
                    StopPrice,
                    OrderQty,
                    Idno,
                    FCM,
                    FFUT_ACCOUNT,
                    SOURCE,
                    CNT,
                    ordno,
                    TradeDate,
                    WEBID,
                    KeyIn,
                    MLEG,
                    Symbol2,
                    BS2,
                    ComYM2,
                    StrikePrice2,
                    CP2):
        return libTradeCom.pyTradeCom_FOrderMulti(self.Obj,
                                                  orderKind,
                                                  orderType,
                                                  RequestId,
                                                  marketFlag,
                                                  BrokerID.encode(),
                                                  Account.encode(),
                                                  AE.encode(),
                                                  EXCHANGE.encode(),
                                                  Symbol.encode(),
                                                  ComYM.encode(),
                                                  StrikePrice.encode(),
                                                  CP.encode(),
                                                  BS.encode(),
                                                  PriceFlag,
                                                  DayTrade.encode(),
                                                  PF,
                                                  TIF,
                                                  OrderPrice.encode(),
                                                  StopPrice.encode(),
                                                  OrderQty,
                                                  Idno.encode(),
                                                  FCM.encode(),
                                                  FFUT_ACCOUNT.encode(),
                                                  SOURCE.encode(),
                                                  CNT.encode(),
                                                  ordno.encode(),
                                                  TradeDate.encode(),
                                                  WEBID.encode(),
                                                  KeyIn.encode(),
                                                  MLEG.encode(),
                                                  Symbol2.encode(),
                                                  BS2.encode(),
                                                  ComYM2.encode(),
                                                  StrikePrice2.encode(),
                                                  CP2.encode())

    #證劵回補回報
    # @param BrokerID  型態:text  分公司代碼
    # @param Account   型態:text  下單帳號
    def ReceiveSReport(self, BrokerID, Account):
        libTradeCom.pyTradeCom_ReceiveSReport(self.Obj, BrokerID.encode(), Account.encode())


    #外期回補回報
    # @param BrokerID  型態:text     分公司代碼
    # @param Account   型態:text     下單帳號
    # @param Trader    型態:text     子帳帳號 (非必要輸入欄位)
    # @param isMulti   型態:Boolean  True:包含複式單回報 False:不包含複式單回報
    def ReceiveFReport(self, BrokerID, Account, Trader, isMulti):
        libTradeCom.pyTradeCom_ReceiveFReport(self.Obj, BrokerID.encode(), Account.encode(), Trader.encode(), isMulti)

    #內期回補回報
    # @param BrokerID  型態:text  分公司代碼
    # @param Account   型態:text  下單帳號
    def ReceiveReport(self, BrokerID, Account):
        libTradeCom.pyTradeCom_ReceiveReport(self.Obj, BrokerID.encode(), Account.encode())


    #期貨分帳庫存彙總查詢 ( 查詢結果 format_id:1616 )
    # @param MType	  型態:text  市場別 
    #                            "O" 國外
    #                            "I" 國內
    # @param BrokerID 型態:text  分公司代號。
    # @param Account  型態:text  帳號。 
    # @param Group    型態:text  第二層組別，一般帳號登入時帶""
    # @param Trader   型態:text  第三層交易員，一般帳號登入時帶""
    def RetrivePositionSum(self, 
                           MType,
                           BrokerID,
                           Account,
                           Group,
                           Trader):
        libTradeCom.pyTradeCom_RetrivePositionSum(self.Obj,
                                                  MType.encode(), 
                                                  BrokerID.encode(), 
                                                  Account.encode(), 
                                                  Group.encode(), 
                                                  Trader.encode())

    #期貨分帳庫存明細查詢 ( 查詢結果 format_id:1618 )
    # @param MType	  型態:text  市場別 
    #                            "O" 國外
    #                            "I" 國內
    # @param BrokerID 型態:text  分公司代號。
    # @param Account  型態:text  帳號。 
    # @param Group    型態:text  第二層組別，一般帳號登入時帶""
    # @param Trader   型態:text  第三層交易員，一般帳號登入時帶""
    def RetrivePositionDetail(self,
                              MType,
                              BrokerID,
                              Account,
                              Group,
                              Trader):
        libTradeCom.pyTradeCom_RetrivePositionDetail(self.Obj, 
                                                     MType.encode(), 
                                                     BrokerID.encode(), 
                                                     Account.encode(), 
                                                     Group.encode(), 
                                                     Trader.encode())

    #期貨權益數查詢 ( 查詢結果 format_id:1626 )
    # @param MType	  型態:text  市場別 
    #                            "O" 國外
    #                            "I" 國內
    # @param BrokerID 型態:text  分公司代號。
    # @param Account  型態:text  帳號。 
    # @param Group    型態:text  第二層組別，一般帳號登入時帶""
    # @param Trader   型態:text  第三層交易員，一般帳號登入時帶""
    def RetriveFMargin(self,
                       MType,
                       BrokerID,
                       Account,
                       Group,
                       Trader):
        libTradeCom.pyTradeCom_RetriveFMargin(self.Obj, 
                                              MType.encode(), 
                                              BrokerID.encode(), 
                                              Account.encode(), 
                                              Group.encode(), 
                                              Trader.encode())

    #期貨權益數查詢_支援人民幣、國內外可出金金額 ( 查詢結果 format_id:1639 )
    # @param MType	  型態:text  市場別 
    #                            "O" 國外
    #                            "I" 國內
    # @param BrokerID 型態:text  分公司代號。
    # @param Account  型態:text  帳號。 
    # @param Group    型態:text  第二層組別，一般帳號登入時帶""
    # @param Trader   型態:text  第三層交易員，一般帳號登入時帶""
    def RetriveFMargin_EX(self,
                          MType,
                          BrokerID,
                          Account,
                          Group,
                          Trader):
        libTradeCom.pyTradeCom_RetriveFMargin_EX(self.Obj, 
                                                 MType.encode(), 
                                                 BrokerID.encode(), 
                                                 Account.encode(), 
                                                 Group.encode(), 
                                                 Trader.encode())
                                                 
    #期貨分帳平倉查詢 ( 查詢結果 format_id:1614 )
    # @param MType	      型態:text  市場別 
    #                                "O" 國外
    #                                "I" 國內
    # @param BrokerID     型態:text  分公司代號。
    # @param Account      型態:text  帳號。 
    # @param Trader       型態:text  第三層交易員，一般帳號登入時帶""
    # @param ComID        型態:text  商品代碼
    # @param ComYM        型態:text  商品年月
    # @param StrikePrice  型態:text  履約價 7位整數, 6位小數
    # @param CP           型態:text  買賣權
    #                                "C" CALL
    #                                "P" PUT 
    #                                ""  期貨
    # @param Exchange     型態:text  交易所
    #                                當查詢條件為全部商品時=> 交易所為 "******"
    #                                當查詢條件為單一商品時=> 交易所、商品代號、商品年月、履約價、買賣權需為正確商品
    # @param Group        型態:text  第二層組別，一般帳號登入時帶""
    def RetriveCOVER(self, 
                     MType,
                     BrokerID,
                     Account,
                     Trader,
                     ComID,
                     ComYM,
                     StrikePrice,
                     CP,
                     Exchange,
                     Group):
        return libTradeCom.pyTradeCom_RetriveCOVER(self.Obj,
                                                   MType.encode(),
                                                   BrokerID.encode(),
                                                   Account.encode(),
                                                   Trader.encode(),
                                                   ComID.encode(),
                                                   ComYM.encode(),
                                                   StrikePrice.encode(),
                                                   CP.encode(),
                                                   Exchange.encode(),
                                                   Group.encode())


    #期貨分帳平倉明細查詢 ( 查詢結果 format_id:1624 )
    # @param MType	      型態:text  市場別 
    #                            "O" 國外
    #                            "I" 國內
    # @param BrokerID     型態:text  分公司代號。
    # @param Account      型態:text  帳號。 
    # @param Trader       型態:text  第三層交易員，一般帳號登入時帶""
    # @param ComID        型態:text  商品代碼
    # @param ComYM        型態:text  商品年月
    # @param StrikePrice  型態:text  履約價 7位整數, 6位小數
    # @param CP           型態:text  買賣權
    #                                 "C" CALL
    #                                 "P" PUT 
    #                                 ""  期貨
    # @param Exchange     型態:text   交易所
    #                                 當查詢條件為全部商品時=> 交易所為 "******"
    #                                 當查詢條件為單一商品時=> 交易所、商品代號、商品年月、履約價、買賣權需為正確商品
    # @param RequestID    型態:number client端對應序號，預設帶 0
    # @param Group        型態:text   第二層組別，一般帳號登入時帶 ""
    def RetriveCOVERDetail(self,
                           MType,
                           BrokerID,
                           Account,
                           Trader,
                           ComID,
                           ComYM,
                           StrikePrice,
                           CP,
                           Exchange,
                           RequestID,
                           Group):
        return libTradeCom.pyTradeCom_RetriveCOVERDetail(self.Obj,
                                                         MType.encode(),
                                                         BrokerID.encode(),
                                                         Account.encode(),
                                                         Trader.encode(),
                                                         ComID.encode(),
                                                         ComYM.encode(),
                                                         StrikePrice.encode(),
                                                         CP.encode(),
                                                         Exchange.encode(),
                                                         RequestID,
                                                         Group.encode())

    #當日交割金額試算查詢 ( 查詢結果 format_id:4302 )
    # @param FType	  型態:text   查詢類別
    #                             "M"  股票+交易類別彙總
    #                             "D"  明細
    #                             "S"  台幣彙總(不含外幣)
    #                             "SS" 彙總多筆(客戶+幣別)
    # @param BrokerID 型態:text   分公司代號。
    # @param Account  型態:text   帳號。 
    # @param Symbol   型態:text   商品代碼。
    def RetrieveWsSettleAmtTrial(self,
                                 FType,
                                 BrokerID,
                                 Account,
                                 Symbol):
        return libTradeCom.pyTradeCom_RetrieveWsSettleAmtTrial(self.Obj,
                                                               FType.encode(), 
                                                               BrokerID.encode(), 
                                                               Account.encode(), 
                                                               Symbol.encode())

    #當日交割金額_沖銷明細(非當沖)查詢 ( 查詢結果 format_id:4304 )
    # @param FType	  型態:text 查詢類別："D":明細
    # @param BrokerID 型態:text 分公司代號。
    # @param Account  型態:text 帳號。 
    # @param Symbol   型態:text 商品代碼。
    # @param MthSeqno 型態:text 資料櫃員序號(由4302取得)(現賣,資賣,券買才會有沖銷明細)。
    def RetrieveWsSettleAmtDetail(self,
                                  FType,
                                  BrokerID,
                                  Account,
                                  Symbol,
                                  MthSeqno):
        return libTradeCom.pyTradeCom_RetrieveWsSettleAmtDetail(self.Obj,
                                                                FType.encode(), 
                                                                BrokerID.encode(), 
                                                                Account.encode(), 
                                                                Symbol.encode(), 
                                                                MthSeqno.encode())

    #交割金額查詢(3日)查詢 ( 查詢結果 format_id:4306 )
    # @param FType    型態:text   查詢類別
    #                             "S"   傳送 1 筆資料(台幣,不含外幣),含當日即時應收付金額試算
    #                             "S1"  傳送 1 筆資料(台幣,不含外幣),若未結帳則不計算當日應收付金額,放0
    #                             "SS"  傳送多筆資料(帳號+幣別),含當日即時應收付金額試算
    #                             "SS1" 傳送多筆資料(帳號+幣別),若未結帳則不計算當日應收付金額,放0
    # @param BrokerID 型態:text   分公司代號。
    # @param Account  型態:text   帳號。 
    def RetrieveWsSettleAmt(self, 
                            FType,
                            BrokerID,
                            Account):
        return libTradeCom.pyTradeCom_RetrieveWsSettleAmt(self.Obj, 
                                                          FType.encode(), 
                                                          BrokerID.encode(), 
                                                          Account.encode())

    #庫存損益及即時維持率試算查詢 ( 查詢結果 format_id:4316 )
    # @param FType    型態:text  查詢類別
    #                            "S"  帳號彙總 (換算成台幣彙總)  (現資券合併)
    #                            "SS" 帳號彙總成多筆 (客戶+幣別)  (現資券合併)
    #                            "M"  股票+交易類別小計 (現資券合併) 
    #                            "M1" 股票+交易類別小計 (資券分開，無現股)
    #                            "D"  明細 (現資券合併) 
    #                            "D1" 明細 (資券分開，無現股)
    # @param BrokerID 型態:text  分公司代號。
    # @param Account  型態:text  帳號。 
    # @param Symbol   型態:text  商品代號。
    # @param DType    型態:text  資料類別: (FTYPE 為"M" 及"D" 時可細分類如下列)
    #                            "A"   全部(今日留存庫存的未實現損益)
    #                            "A0"  現股(今日留存庫存的未實現損益,含現賣庫存)
    #                            "A3"  融資(今日留存庫存的未實現損益)
    #                            "A4"  融券(今日留存庫存的未實現損益)
    #                            "B"   當日新增(今日新增庫存)
    #                            "B0"  現股當日新增(今日新增庫存,含現賣庫存)
    #                            "B3"  融資當日新增(今日新增庫存)
    #                            "B4"  融券當日新增(今日新增庫存)
    #                            "S"   現賣
    def RetrieveWsInventory(self,
                            FType,
                            BrokerID,
                            Account,
                            Symbol,
                            DType):
        return libTradeCom.pyTradeCom_RetrieveWsInventory(self.Obj, 
                                                          FType.encode(), 
                                                          BrokerID.encode(), 
                                                          Account.encode(), 
                                                          Symbol.encode(), 
                                                          DType.encode())

    #證券庫存彙總查詢 ( 查詢結果 format_id:4310 )
    # @param FType     型態:text  查詢類別
    #                             "A"  現股以張數顯示 
    #                             "B"  現股以股數顯示
    # @param BrokerID  型態:text  分公司代號。
    # @param Account   型態:text  帳號。 
    # @param Symbol    型態:text  商品代號。
    def RetrieveWsInventorySum(self, 
                               FType,
                               BrokerID,
                               Account, 
                               Symbol):
        return libTradeCom.pyTradeCom_RetrieveWsInventorySum(self.Obj, 
                                                             FType.encode(), 
                                                             BrokerID.encode(), 
                                                             Account.encode(), 
                                                             Symbol.encode())
    #對照單查詢 ( 查詢結果 format_id:4312 )
    # @param FType      型態:text   查詢類別
    #                               "D" 明細 
    #                               "M" 日彙總(不含外幣)
    # @param BrokerID   型態:text   分公司代號
    # @param Account    型態:text   帳號 
    # @param Symbol     型態:text   商品代號
    # @param DType	    型態:text   資料類別 
    #                               "T" 交易 
    #                               "N" 非交易
    # @param StartDate	型態:text  查詢日起 yyyymmdd(若qdays > 0 時,此值失去作用)
    # @param EndDate	型態:text  查詢日迄 yyyymmdd
    # @param QDays	    型態:text  查詢天數, EndDate 前之交易天數，最多不超過30天
    #                              若要查詢 StartDate~EndDate 值必須為 0 
    #                              若不為 0 時, 表示要查詢 EndDate 前的天數, StartDate 值就不會有作用。
    #                              EX: 若輸入 20160301~20160331 , 天數10天, 那麼就只會有 3/31 前10天的資料, 而非 3/1~3/31 資料
    def RetrieveWsBalanceStatement(self,
                                   FType,
                                   BrokerID,
                                   Account,
                                   Symbol,
                                   DType,
                                   StartDate,
                                   EndDate,
                                   QDays):
        return libTradeCom.pyTradeCom_RetrieveWsBalanceStatement(self.Obj,
                                                                 FType.encode(),
                                                                 BrokerID.encode(),
                                                                 Account.encode(),
                                                                 Symbol.encode(),
                                                                 DType.encode(),
                                                                 StartDate.encode(),
                                                                 EndDate.encode(),
                                                                 QDays)

    #帳中已實現損益查詢 ( 查詢結果 format_id:4314 )
    # @param MType     型態:text   查詢類別
    #                              "D" 明細 
    #                              "M" 日彙總(不含外幣)
    # @param BrokerID  型態:text   分公司代號
    # @param Account   型態:text   帳號 
    # @param Symbol    型態:text   商品代號
    # @param DType     型態:text   資料類別 
    #                              "T" 交易 
    #                              "N" 非交易
    # @param StartDate	型態:text  查詢日起 yyyymmdd(若qdays > 0 時,此值失去作用)
    # @param EndDate   型態:text   查詢日迄 yyyymmdd
    # @param QDays     型態:text   查詢天數, EndDate 前之交易天數，最多不超過30天;
    #                              若要查詢 StartDate ~ EndDate 時, 值必須為 0 
    #                              若不為 0 時, 表示要查詢 EndDate 前的天數,查詢日 StartDate 就不會有作用。
    #                              EX: 若輸入 20160301~20160331 , 天數10天, 那麼就只會有 3/31 前10天的資料, 而非 3/1~3/31 資料
    def RetrieveWsRealizePL(self, 
                            FType,
                            BrokerID,
                            Account,
                            Symbol, 
                            DType,
                            StartDate,
                            EndDate,
                            QDays):
        return libTradeCom.pyTradeCom_RetrieveWsRealizePL(self.Obj,
                                                          FType.encode(),
                                                          BrokerID.encode(),
                                                          Account.encode(),
                                                          Symbol.encode(),
                                                          DType.encode(),
                                                          StartDate.encode(),
                                                          EndDate.encode(),
                                                          QDays)        
    #複委託委託查詢 ( 查詢結果 format_id:4113 )
    # @param brokerid  型態:text   分公司
    # @param Account   型態:text   帳號
    # @param Market    型態:number 市場別
    #                               0 港股  
    #                               1 美股
    #                               2 滬股通
    #                               3 日股
    def RetrieveRcOrderReport(self,
                              brokerid,
                              account,
                              market):
        return libTradeCom.pyTradeCom_RetrieveRcOrderReport(self.Obj,
                                                            brokerid.encode(), 
                                                            account.encode(),  
                                                            market)

    #複委託成交彙總查詢 ( 查詢結果 format_id:4115 )
    # @param brokerid  型態:text   分公司
    # @param Account   型態:text   帳號
    # @param Market    型態:number 市場別
    #                               0 港股  
    #                               1 美股
    #                               2 滬股通
    #                               3 日股
    def RetrieveRcMatchReportSum(self,
                                 brokerid,
                                 account,
                                 market):
        return libTradeCom.pyTradeCom_RetrieveRcMatchReportSum(self.Obj, 
                                                               brokerid.encode(),
                                                               account.encode(),
                                                               market)

    #複委託成交明細查詢 ( 查詢結果 format_id:4117 )
    # @param brokerid  型態:text   分公司
    # @param Account   型態:text   帳號
    # @param Market    型態:number 市場別
    #                               0 港股  
    #                               1 美股
    #                               2 滬股通
    #                               3 日股
    def RetrieveRcMatchReportDetail(self,
                                    brokerid,
                                    account,
                                    market):
        return libTradeCom.pyTradeCom_RetrieveRcMatchReportDetail(self.Obj,
                                                                  brokerid.encode(),
                                                                  account.encode(),
                                                                  market)

    #複委託整戶部位明細查詢 ( 查詢結果 format_id:4119 )
    # @param brokerid  型態:text   分公司
    # @param Account   型態:text   帳號
    def RetrieveRcPositionDetailReport(self,
                                       brokerid,
                                       account):
        return libTradeCom.pyTradeCom_RetrieveRcPositionDetailReport(self.Obj, brokerid.encode(), account.encode())

    #複委託單一幣別帳務查詢 ( 查詢結果 format_id:4121 )
    # @param brokerid  型態:text   分公司
    # @param Account   型態:text   帳號
    # @param readecurr 型態:text   交易幣別 (HKD,USD,NTD.....)
    def RetrieveRcCurrencyReport(self, 
                                 brokerid,
                                 account,
                                 tradecurr):
        return libTradeCom.pyTradeCom_RetrieveRcCurrencyReport(self.Obj, brokerid.encode(), account.encode(), tradecurr.encode())

    #複委託股票庫存部位查詢 ( 查詢結果 format_id:4123 )
    # @param BrokerID 型態:text    分公司
    # @param Account  型態:text    帳號
    # @param Market   型態:number  市場別
    #                               0 港股  
    #                               1 美股
    #                               2 滬股通
    #                               3 日股
    # @param protype  型態:number  商品類別
    #                               0 股票
    #                               1 基金
    #                               2 債劵
    def RetrieveRcStockPositionReport(self,
                                      brokerid,
                                      account,
                                      market,
                                      protype):
        return libTradeCom.pyTradeCom_RetrieveRcStockPositionReport(self.Obj, 
                                                                    brokerid.encode(), 
                                                                    account.encode(), 
                                                                    market,
                                                                    protype)

    #複委託交割金額試算 ( 查詢結果 format_id:4125 )
    # @param brokerid  型態:text   分公司
    # @param Account   型態:text   帳號
    # @param symbol    型態:text   商品代碼
    def RetrieveRcDeliveryReport(self, 
                                 brokerid,
                                 account,
                                 symbol):
        return libTradeCom.pyTradeCom_RetrieveRcDeliveryReport(self.Obj, 
                                                               brokerid.encode(),
                                                               account.encode(),
                                                               symbol.encode())
    
    #證券-資券餘額查詢 ( 查詢結果 format_id:4033 )
    # @param brokerid  型態:text    分公司
    # @param type      型態:number  查詢類別 0:類股 1:股票
    # @param qno       型態:text    類別代碼(查詢類股時) or 股票代碼(查詢股票時) 
    def RetrieveTSECreditInfo(self,
                              brokerid,
                              type,
                              qno):
        return libTradeCom.pyTradeCom_RetrieveTSECreditInfo(self.Obj, 
                                                            brokerid.encode(),
                                                            type,
                                                            qno.encode())

    #下載複委託商品檔
    def RetriveSecProduct(self):
        return libTradeCom.pyTradeCom_RetriveSecProduct(self.Obj)
     
    #下載海外期貨商品檔
    def RetrieveFProduct(self):
        return libTradeCom.pyTradeCom_RetrieveFProduct(self.Obj)
    
    # ----- event callback ------------------------------------------------------------------------
    
    # status 0:disconnect    連線中斷
    #        1:connect_fail  連線失敗
    #        3:connected     連線成功
    #        4:logon_ready   登入成功
    #        5:logon_fail    登入失敗
    def OnStatusChanged(self, status):
        pass
    
    # format_id 6002: 證劵委託收單回應
    #           4102: 複委託收單回應  
    #           2002: 國內期權收單回應  
    #           3302: 國外期權收單回應      
    def OnOrderPending(self, format_id, response):
        pass
    
    # format_id 4010: 證劵委託回報
    #           4110: 複委託委託回報  
    #           2010: 國內期權委託回報  
    #           3310: 國外期權單式單委託回報 
    #           3314: 國外期權複式單委託回報 
    def OnOrderReport(self, format_id, response):
        pass
    
    # format_id 4011: 證劵成交回報
    #           4111: 複委託成交回報  
    #           2011: 國內期權成交回報  
    #           3311: 國外期權單式單成交回報 
    #           3315: 國外期權複式單成交回報 
    def OnExecReport(self, format_id, response):
        pass

    # format_id 1616:期貨分帳庫存彙總查詢結果                          #profit
    #           1618:期貨分帳庫存明細查詢結果                          #profit_detail
    #           1626:期貨權益數查詢結果                                #balance
    #           1639:期貨權益數_支援人民幣、國內外可出金金額查詢結果
    #           1614:期貨分帳平倉查詢結果                              #realprofit
    #           1624:期貨分帳平倉明細查詢結果                          #realprofit_detail
    #           4302:當日交割金額試算查詢結果               #realprofit
    #           4304:當日交割金額_沖銷明細(非當沖)查詢結果   #settlement_td
    #           4306:交割金額查詢(3日)查詢結果              #settlement_3d
    #           4316:庫存損益及即時維持率試算查詢結果        #profit_realize
    #           4310:證券庫存彙總查詢結果                   #profit
    #           4312:對照單查詢結果                        #statement
    #           4314:帳中已實現損益查詢結果                 #realprofit_realize
    #           4113:複委託委託查詢結果                ##回補order
    #           4115:複委託成交彙總查詢結果       
    #           4117:複委託成交明細查詢結果            ##回補deal
    #           4119:複委託整戶部位明細查詢結果        #statement  
    #           4121:複委託單一幣別帳務查詢結果        #statement
    #           4123:複委託股票庫存部位查詢結果        #profit
    #           4125:複委託交割金額試算查詢結果        #settlement
    #           4033:證券 - 資券餘額查詢結果
    def OnQueryResult(self, format_id, response):
        pass
        
#----------------------------------------------------------------------------

