from futu import SysConfig, OpenSecTradeContext, TrdMarket
SysConfig.set_init_rsa_file("/secrets/futu_conn_key.pem")
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.HK, host="futu-opend", port=11111, is_encrypt=True)
ret, msg = ctx.unlock_trade(password_md5="deadbeef"*4)
print("RESULT ret=", ret, "msg=", str(msg)[:300])
ctx.close()
