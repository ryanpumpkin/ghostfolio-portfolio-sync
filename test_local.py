from futu import OpenSecTradeContext, TrdMarket
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.HK, host="127.0.0.1", port=11111)
ret, msg = ctx.unlock_trade(password_md5="deadbeef"*4)
print("RESULT ret=", ret, "msg=", str(msg)[:300])
ctx.close()
