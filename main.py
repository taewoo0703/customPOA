from fastapi.exception_handlers import (
    request_validation_exception_handler,
)
from pprint import pprint
from fastapi import FastAPI, Request, status, BackgroundTasks
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
import httpx
from exchange.stock.kis import KoreaInvestment
from exchange.model import MarketOrder, PriceRequest, HedgeData, OrderRequest, ArbiData, HatikoInfo
from exchange.utility import (
    settings,
    log_order_message,
    log_alert_message,
    print_alert_message,
    logger_test,
    log_order_error_message,
    log_validation_error_message,
    log_hedge_message,
    log_error_message,
    log_message,
	log_custom_message,
    log_arbi_message
)
import traceback
from exchange import get_exchange, log_message, db, settings, get_bot, pocket
import ipaddress
import os
import sys
from devtools import debug

VERSION = "POA : 0.1.1, Hatiko : 230922 23:00"
app = FastAPI(default_response_class=ORJSONResponse)


def get_error(e):
    tb = traceback.extract_tb(e.__traceback__)
    target_folder = os.path.abspath(os.path.dirname(tb[0].filename))
    error_msg = []

    for tb_info in tb:
        # if target_folder in tb_info.filename:
        error_msg.append(f"File {tb_info.filename}, line {tb_info.lineno}, in {tb_info.name}")
        error_msg.append(f"  {tb_info.line}")

    error_msg.append(str(e))

    return error_msg


@app.on_event("startup")
async def startup():
    log_message(f"POABOT 실행 완료! - 버전:{VERSION}")


@app.on_event("shutdown")
async def shutdown():
    db.close()


whitelist = ["52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7", "127.0.0.1"]
whitelist = whitelist + settings.WHITELIST


# @app.middleware("http")
# async def add_process_time_header(request: Request, call_next):
#     start_time = time.perf_counter()
#     response = await call_next(request)
#     process_time = time.perf_counter() - start_time
#     response.headers["X-Process-Time"] = str(process_time)
#     return response


@app.middleware("http")
async def whitelist_middleware(request: Request, call_next):
    try:
        if request.client.host not in whitelist and not ipaddress.ip_address(request.client.host).is_private:
            msg = f"{request.client.host}는 안됩니다"
            print(msg)
            return ORJSONResponse(status_code=status.HTTP_403_FORBIDDEN, content=f"{request.client.host}는 허용되지 않습니다")
    except:
        log_error_message(traceback.format_exc(), "미들웨어 에러")
    else:
        response = await call_next(request)
        return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    msgs = [f"[에러{index+1}] " + f"{error.get('msg')} \n{error.get('loc')}" for index, error in enumerate(exc.errors())]
    message = "[Error]\n"
    for msg in msgs:
        message = message + msg + "\n"

    log_validation_error_message(f"{message}\n {exc.body}")
    return await request_validation_exception_handler(request, exc)


@app.get("/ip")
async def get_ip():
    data = httpx.get("https://ipv4.jsonip.com").json()["ip"]
    log_message(data)


@app.get("/hi")
async def welcome():
    return "hi!!"


@app.post("/price")
async def price(price_req: PriceRequest, background_tasks: BackgroundTasks):
    exchange = get_exchange(price_req.exchange)
    price = exchange.dict()[price_req.exchange].fetch_price(price_req.base, price_req.quote)
    return price


def log(exchange_name, result, order_info):
    log_order_message(exchange_name, result, order_info)
    print_alert_message(order_info)


def log_error(error_message, order_info):
    log_order_error_message(error_message, order_info)
    log_alert_message(order_info, "실패")


@app.post("/order")
@app.post("/")
async def order(order_info: MarketOrder, background_tasks: BackgroundTasks):
    order_result = None
    try:
        exchange_name = order_info.exchange
        bot = get_bot(exchange_name, order_info.kis_number)
        bot.init_info(order_info)

        if bot.order_info.is_crypto:
            if bot.order_info.is_entry:
                order_result = bot.market_entry(bot.order_info)
            elif bot.order_info.is_close:
                order_result = bot.market_close(bot.order_info)
            elif bot.order_info.is_buy:
                order_result = bot.market_buy(bot.order_info)
            elif bot.order_info.is_sell:
                order_result = bot.market_sell(bot.order_info)
            background_tasks.add_task(log, exchange_name, order_result, order_info)
        elif bot.order_info.is_stock:
            order_result = bot.create_order(
                bot.order_info.exchange,
                bot.order_info.base,
                order_info.type.lower(),
                order_info.side.lower(),
                order_info.amount,
            )
            background_tasks.add_task(log, exchange_name, order_result, order_info)

    except TypeError as e:
        error_msg = get_error(e)
        background_tasks.add_task(log_order_error_message, "\n".join(error_msg), order_info)

    except Exception as e:
        error_msg = get_error(e)
        background_tasks.add_task(log_error, "\n".join(error_msg), order_info)

    else:
        return {"result": "success"}

    finally:
        pass


def get_hedge_records(base):
    records = pocket.get_full_list("kimp", query_params={"filter": f'base = "{base}"'})
    binance_amount = 0.0
    binance_records_id = []
    upbit_amount = 0.0
    upbit_records_id = []
    for record in records:
        if record.exchange == "BINANCE":
            binance_amount += record.amount
            binance_records_id.append(record.id)
        elif record.exchange == "UPBIT":
            upbit_amount += record.amount
            upbit_records_id.append(record.id)

    return {
        "BINANCE": {"amount": binance_amount, "records_id": binance_records_id},
        "UPBIT": {"amount": upbit_amount, "records_id": upbit_records_id},
    }


@app.post("/hedge")
async def hedge(hedge_data: HedgeData, background_tasks: BackgroundTasks):
    exchange_name = hedge_data.exchange.upper()
    bot = get_bot(exchange_name)
    upbit = get_bot("UPBIT")

    base = hedge_data.base
    quote = hedge_data.quote
    amount = hedge_data.amount
    leverage = hedge_data.leverage
    hedge = hedge_data.hedge

    foreign_order_info = OrderRequest(
        exchange=exchange_name,
        base=base,
        quote=quote,
        side="entry/sell",
        type="market",
        amount=amount,
        leverage=leverage,
    )
    bot.init_info(foreign_order_info)
    if hedge == "ON":
        try:
            if amount is None:
                raise Exception("헷지할 수량을 요청하세요")
            binance_order_result = bot.market_entry(foreign_order_info)
            binance_order_amount = binance_order_result["amount"]
            pocket.create("kimp", {"exchange": "BINANCE", "base": base, "quote": quote, "amount": binance_order_amount})
            if leverage is None:
                leverage = 1
            try:
                korea_order_info = OrderRequest(
                    exchange="UPBIT",
                    base=base,
                    quote="KRW",
                    side="buy",
                    type="market",
                    amount=binance_order_amount,
                )
                upbit.init_info(korea_order_info)
                upbit_order_result = upbit.market_buy(korea_order_info)
            except Exception as e:
                hedge_records = get_hedge_records(base)
                binance_records_id = hedge_records["BINANCE"]["records_id"]
                binance_amount = hedge_records["BINANCE"]["amount"]
                binance_order_result = bot.market_close(
                    OrderRequest(
                        exchange=exchange_name, base=base, quote=quote, side="close/buy", amount=binance_amount
                    )
                )
                for binance_record_id in binance_records_id:
                    pocket.delete("kimp", binance_record_id)
                log_message("[헷지 실패] 업비트에서 에러가 발생하여 바이낸스 포지션을 종료합니다")
            else:
                upbit_order_info = upbit.get_order(upbit_order_result["id"])
                upbit_order_amount = upbit_order_info["filled"]
                pocket.create("kimp", {"exchange": "UPBIT", "base": base, "quote": "KRW", "amount": upbit_order_amount})
                log_hedge_message(exchange_name, base, quote, binance_order_amount, upbit_order_amount, hedge)

        except Exception as e:
            # log_message(f"{e}")
            background_tasks.add_task(log_error_message, traceback.format_exc(), "헷지 에러")
            return {"result": "error"}
        else:
            return {"result": "success"}

    elif hedge == "OFF":
        try:
            records = pocket.get_full_list("kimp", query_params={"filter": f'base = "{base}"'})
            binance_amount = 0.0
            binance_records_id = []
            upbit_amount = 0.0
            upbit_records_id = []
            for record in records:
                if record.exchange == "BINANCE":
                    binance_amount += record.amount
                    binance_records_id.append(record.id)
                elif record.exchange == "UPBIT":
                    upbit_amount += record.amount
                    upbit_records_id.append(record.id)

            if binance_amount > 0 and upbit_amount > 0:
                # 바이낸스
                order_info = OrderRequest(
                    exchange="BINANCE", base=base, quote=quote, side="close/buy", amount=binance_amount
                )
                binance_order_result = bot.market_close(order_info)
                for binance_record_id in binance_records_id:
                    pocket.delete("kimp", binance_record_id)
                # 업비트
                order_info = OrderRequest(exchange="UPBIT", base=base, quote="KRW", side="sell", amount=upbit_amount)
                upbit_order_result = upbit.market_sell(order_info)
                for upbit_record_id in upbit_records_id:
                    pocket.delete("kimp", upbit_record_id)

                log_hedge_message(exchange_name, base, quote, binance_amount, upbit_amount, hedge)
            elif binance_amount == 0 and upbit_amount == 0:
                log_message(f"{exchange_name}, UPBIT에 종료할 수량이 없습니다")
            elif binance_amount == 0:
                log_message(f"{exchange_name}에 종료할 수량이 없습니다")
            elif upbit_amount == 0:
                log_message("UPBIT에 종료할 수량이 없습니다")
        except Exception as e:
            background_tasks.add_task(log_error_message, traceback.format_exc(), "헷지종료 에러")
            return {"result": "error"}
        else:
            return {"result": "success"}







##############################################################################
# by PTW
##############################################################################
import threading
import time
import queue

#region Flags
USE_DISCORD = False # Discord 사용 여부
LOG = False # LOG 찍어보기 Flag

# Discord 변경
@ app.get("/change_discord")
async def change_discord():
    global USE_DISCORD
    USE_DISCORD = not USE_DISCORD
    return f"USE_DISCORD : {USE_DISCORD}"


# LOG Flag 변경
@ app.get("/change_log")
async def change_log():
    global LOG
    LOG = not LOG
    return f"LOG : {LOG}"

#endregion Flags



#region 유효성 검증 후의 order_info 보기 
@ app.post("/orderinfo")
@ app.post("/")
async def orderinfo(order_info: MarketOrder, background_tasks: BackgroundTasks):
    res = {
        "exchange(Literal str)"  : str(order_info.exchange),
        "base(str)" : str(order_info.base),
        "quote(Literal str)" : str(order_info.quote),
        "type(Literal str)" : str(order_info.type),
        "side(Literal str)" : str(order_info.side),
        "amount(float)" : str(order_info.amount),
        "price(float)" : str(order_info.price),
        "cost(float)" : str(order_info.cost),
        "percent(float)" : str(order_info.percent),
        "amount_by_percent(float)" : str(order_info.amount_by_percent),
        "leverage(int)" : str(order_info.leverage),
        "stop_price(float)" : str(order_info.stop_price),
        "profit_price(float)" : str(order_info.profit_price),
        "order_name(str)" : str(order_info.order_name),
        "kis_number(int)" : str(order_info.kis_number),
        "hedge(str)" : str(order_info.hedge),
        "unified_symbol(str)" : str(order_info.unified_symbol),
        "is_crypto(bool)" : str(order_info.is_crypto),
        "is_stock(bool)" : str(order_info.is_stock),
        "is_spot(bool)" : str(order_info.is_spot),
        "is_futures(bool)" : str(order_info.is_futures),
        "is_coinm(bool)" : str(order_info.is_coinm),
        "is_entry(bool)" : str(order_info.is_entry),
        "is_close(bool)" : str(order_info.is_close),
        "is_buy(bool)" : str(order_info.is_buy),
        "is_sell(bool)" : str(order_info.is_sell),
        "is_contract(bool)" : str(order_info.is_contract),
        "contract_size(float)" : str(order_info.contract_size),
        "margin_mode(str)" : str(order_info.margin_mode)
        }
    return res
#endregion 유효성 검증 후의 order_info 보기 



#region ############################### Hatiko ###############################

#region Hatiko용 Flag, 전역변수
USE_HATIKO = True # Hatiko 사용 여부
KILL_CONFIRM = True # 시간차 시장가청산 사용 여부
KILL_MINUTE = 10 # 시간차 시장가 청산 기능에서 대기 시간(분)


# USE_HATIKO 변경
@ app.get("/change_hatiko")
async def change_hatiko():
    global USE_HATIKO
    USE_HATIKO = not USE_HATIKO
    return f"USE_HATIKO : {USE_HATIKO}"

# KILL_CONFIRM 변경
@ app.get("/change_kill_confirm")
async def change_kill_confirm():
    global KILL_CONFIRM
    KILL_CONFIRM = not KILL_CONFIRM
    return f"KILL_CONFIRM : {KILL_CONFIRM}"

# KILL_MINUTE 변경
@ app.get("/set_kill_minute/{minute}")
async def set_kill_minute(minute: int):
    global KILL_MINUTE
    KILL_MINUTE = minute
    return f"KILL_MINUTE : {KILL_MINUTE}"

#endregion Hatiko용 Flag, 전역변수


# 실매매용 웹훅URL
@ app.post("/hatiko")
@ app.post("/")
async def add_hatiko_queue(order_info: MarketOrder):
    hatiko_queue.put(order_info)
    return {"result": "success"}


#region 각 거래소별 HatikoInfo
HI_Binance_Spot     = HatikoInfo(nMaxLong=10, nMaxShort=1, nIgnoreLong=0, nIgnoreShort=0) 
HI_Binance_Future   = HatikoInfo(nMaxLong=2, nMaxShort=1, nIgnoreLong=1, nIgnoreShort=0)
HI_OKX_Spot         = HatikoInfo(nMaxLong=10, nMaxShort=1, nIgnoreLong=0, nIgnoreShort=0)
HI_OKX_Future       = HatikoInfo(nMaxLong=2, nMaxShort=1, nIgnoreLong=1, nIgnoreShort=0)
HI_Bitget_Spot      = HatikoInfo(nMaxLong=10, nMaxShort=1, nIgnoreLong=0, nIgnoreShort=0)
HI_Bitget_Future    = HatikoInfo(nMaxLong=2, nMaxShort=1, nIgnoreLong=1, nIgnoreShort=0)
HI_Bybit_Spot       = HatikoInfo(nMaxLong=10, nMaxShort=1, nIgnoreLong=0, nIgnoreShort=0)
HI_Bybit_Future     = HatikoInfo(nMaxLong=2, nMaxShort=1, nIgnoreLong=1, nIgnoreShort=0)

hatikoInfoObjects = {
    "binance_spot": HI_Binance_Spot,
    "binance_future": HI_Binance_Future,
    "okx_spot": HI_OKX_Spot,
    "okx_future": HI_OKX_Future,
    "bitget_spot": HI_Bitget_Spot,
    "bitget_future": HI_Bitget_Future,
    "bybit_spot": HI_Bybit_Spot,
    "bybit_future": HI_Bybit_Future,
}

#endregion 각 거래소별 HatikoInfo



#region HatikoInfo 관련 함수
# HatikoInfo 관련 메모리 모니터
@app.get("/hatikoinfo/{exchange}/{market_type}")
async def get_hatikoinfo(exchange: str, market_type: str):
    hatikoInfo = hatikoInfoObjects.get(f"{exchange}_{market_type}")
    if hatikoInfo:
        return hatikoInfo.getHatikoInfo()
    else:
        return {"error": "해당 거래소 또는 상품 유형의 정보를 찾을 수 없습니다."}

# HatikoInfo 리셋
@app.get("/reset_hatikoinfo/{exchange}/{productType}")
async def reset_hatikoinfo(exchange: str, productType: str):
    hatikoInfo = hatikoInfoObjects.get(f"{exchange}_{productType}")
    with hatiko_lock:
        if hatikoInfo:
            hatikoInfo.resetHatikoInfo()
            return "Reset HatikoInfo Complete!!!"
        else:
            return {"error": "해당 거래소 또는 상품 유형의 HatikoInfo 객체를 찾을 수 없습니다."}

@app.get("/reset_hatikoinfo_all")
async def reset_hatikoinfo_all():
    with hatiko_lock:
        for hatikoInfo in hatikoInfoObjects.values():
            hatikoInfo.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

# set nMax, nIgnore
@app.get("/set_hatikoinfo/{exchange}/{market_type}/{variable}/{value}")
async def set_hatikoinfo(exchange: str, market_type: str, variable: str, value: int):
    global hatikoInfoObjects
    if variable not in ("nmax_long", "nmax_short", "nignore_long", "nignore_short", "liquidation_mdd"):
        return "Invalid variable."
    hatikoInfo = hatikoInfoObjects.get(f"{exchange}_{market_type}")
    with hatiko_lock:
        if hatikoInfo:
            hatikoInfo.set_variable(variable, value)
            return f"Set {variable} : {value}"
        else:
            return "Invalid exchange."
    
# set nMax, nIgnore
@app.get("/set_hatikoinfo_all/{variable}/{value}")
async def set_hatikoinfo_all(variable: str, value: int):
    global hatikoInfoObjects
    if variable not in ("nmax_long", "nmax_short", "nignore_long", "nignore_short", "liquidation_mdd"):
        return "Invalid variable."
    with hatiko_lock:
        for hatikoInfo in hatikoInfoObjects.values():
            hatikoInfo.set_variable(variable, value)
        return f"Set {variable} : {value}"

#endregion HatikoInfo 관련 함수



#region HatikoBase 관련 함수
def updateOrderInfo(order_info: MarketOrder, amount: float=None, percent: float=None, price: float=None, 
                    side=None, is_entry: bool=None, is_close: bool=None, is_buy: bool=None, is_sell: bool=None,
                    leverage: int=None, order_name: str=None):
    """
    customPOA는 order_info의 amount, percent, price에 구애받지 않고 거래하는 경우가 많다.
    이런 경우 log와 실매매간의 괴리가 있을 수 있으며, 경우에 따라 log가 출력되지 않는다.
    이를 해결하기 위해 log를 찍기 전에 이 메소드를 사용하여 log 출력을 보장한다.
    """
    if amount is not None:
        order_info.amount = amount
    if percent is not None:
        order_info.percent = percent
    if price is not None:
        order_info.price = price
    if side is not None:
        order_info.side = side
    if is_entry is not None:
        order_info.is_entry = is_entry
    if is_close is not None:
        order_info.is_close = is_close
    if is_buy is not None:
        order_info.is_buy = is_buy
    if is_sell is not None:
        order_info.is_sell = is_sell
    if leverage is not None:
        order_info.leverage = leverage
    if order_name is not None:
        order_info.order_name = order_name

def getMinMaxQty(bot, order_info: MarketOrder) -> (float, float):
    """
    주문 시 최대, 최소 수량을 구하는 방법이 거래소마다 다름.
    max_amount : 지정가 주문 최대 코인개수 -> 100,000 달러를 기준으로 할까? -> 거래소에서 주는 값과 비교하여 높은 값으로 선정
    min_amount : 지정가 주문 최소 코인개수 -> 10 달러를 기준으로 한다! -> 거래소에서 주는 값과 비교하여 높은 값으로 선정
    return (최대수량, 최소수량)
    """
    price = order_info.price
    max_cash = 100000   # 100,000달러
    min_cash = 10       # 10달러
    max_amount = max_cash / price
    min_amount = min_cash / price

    market = bot.client.market(order_info.unified_symbol)
    if order_info.exchange in "BINANCE":
        max_amount = market["limits"]["amount"]["max"] if market["limits"]["amount"]["max"] > max_amount else max_amount
        min_amount = market["limits"]["amount"]["min"] if market["limits"]["amount"]["min"] > min_amount else min_amount
    elif order_info.exchange == "BYBIT":
        max_amount = market["limits"]["amount"]["max"] if market["limits"]["amount"]["max"] > max_amount else max_amount
        min_amount = market["limits"]["amount"]["min"] if market["limits"]["amount"]["min"] > min_amount else min_amount
    elif order_info.exchange == "BITGET":
        max_amount = max_amount # bitget은 최대수량이 없음
        min_amount = min_amount # bitget은 최소수량이 없음
    elif order_info.exchange == "OKX":
        max_amount = float(market["info"]["maxLmtSz"]) if float(market["info"]["maxLmtSz"]) > max_amount else max_amount
        min_amount = float(market["info"]["minSz"]) if float(market["info"]["minSz"]) > min_amount else min_amount
    
    return max_amount, min_amount
            
def removeItemFromMultipleDicts(item, *dicts):
    for dic in dicts:
        if item in dic:
            dic.pop(item)

def removeItemFromMultipleLists(item, *lists) -> bool:
    is_removed = False
    for _list in lists:
        if item in _list:
            _list.remove(item)
            is_removed = True
    if is_removed:
        return True
    else:
        return False

def kill_confirm_thread_func(order_info: MarketOrder):
    # N분 대기
    time.sleep(60 * KILL_MINUTE)
    # order_name을 시장가로 변경 후 queue에 넣기
    updateOrderInfo(order_info, order_name="Kill_Confirm")
    hatiko_queue.put(order_info)

#endregion HatikoBase 관련 함수



#region Hatiko Thread

# 작업을 저장할 큐 생성
hatiko_queue = queue.Queue()

# 스레드 간에 안전하게 공유되는 Lock 객체 생성
hatiko_lock = threading.Lock()

# 별도의 스레드에서 동기적으로 작업을 처리하는 함수
def hatiko_thread_proc(hatiko_queue):
    while USE_HATIKO:
        order_info = hatiko_queue.get()
        with hatiko_lock:
            hatiko(order_info)

# Hatiko 스레드를 시작
hatiko_thread = threading.Thread(target=hatiko_thread_proc, args=(hatiko_queue, ))
hatiko_thread.start()

#endregion Hatiko Thread



#region Hatiko Main Function


def hatiko(order_info: MarketOrder):
    global HI_Binance_Future, HI_Binance_Spot, HI_Bitget_Future, HI_Bitget_Spot, HI_Bybit_Future, HI_Bybit_Spot, HI_OKX_Future, HI_OKX_Spot
    
    exchange = order_info.exchange
    is_spot = order_info.is_spot
    hatikoInfo = None

    if exchange == "BINANCE":
        hatikoInfo = HI_Binance_Spot if is_spot else HI_Binance_Future
    elif exchange == "BITGET":
        hatikoInfo = HI_Bitget_Spot if is_spot else HI_Bitget_Future
    elif exchange == "BYBIT":
        hatikoInfo = HI_Bybit_Spot if is_spot else HI_Bybit_Future
    elif exchange == "OKX":
        hatikoInfo = HI_OKX_Spot if is_spot else HI_OKX_Future
    else:
        return "Invalid exchange."
    
    if hatikoInfo:
        hatikoBase(order_info, hatikoInfo)

def hatikoBase(order_info: MarketOrder, hatikoInfo: HatikoInfo):
    """
    지정가 Hatiko 전략

    [트뷰]
    nearLong1 : Long1 가격 근처에 갔을 때 발생. Long1 가격을 전달함.
    Long1 : Long1 가격 도달 시 발생
    NextCandle_L1 : nearLong1 시그널 발생 후 청산 전까지 봉마감 할 때마다 발생. 새로운 Level_Long1 가격을 전달함.
    Close 및 Exit : 청산 조건 달성 시 발생

    [하티코봇]
    1. nearLong1 시그널 수신
    nearLong1_list 최대개수 확인 -> 미달 시, 지정가 매수주문 -> 성공 시, nearLong1_list에 추가

    2. NextCandle_L1 시그널 수신
    해당 종목이 nearLong1_list에 존재하는지 확인 -> 존재 시, Long1_list에 없으면 미체결주문 체크 -> 미체결주문 취소 & 신규 Long1 주문

    3. Long1 시그널 수신
    해당 종목이 nearLong1_list에 존재하는지 확인 -> 존재 시, Long1 리스트에 추가

    4. 청산 시그널 수신
    해당 종목이 nearLong1_list에 존재하는지 확인 -> 존재 시, 청산 주문 -> 성공 시, 존재하는 모든 리스트에서 제거
    
    5. Kill_Confirm 시그널 수신 (시간차 시장가 청산 기능)
    1분 대기 -> 모든 미체결 청산주문 확인 -> 미체결 주문 있으면 취소 -> remaining amount 만큼 청산
    
    """
    
    # 초기화 단계
    order_result = None
    nMaxTry = 5                # 주문 재시도 횟수
    nGoal = 0   
    nComplete = 0
    isSettingFinish = False     # 매매전 ccxt 세팅 flag 
    orderID_list = []           # 오더id 리스트
    isCancelSuccess = False     # 미체결주문 취소성공 여부
    isReEntry = False           # 재진입 필요여부
    isOrderSuccess = False      # 주문 성공 여부
    amountCanceled = 0.0        # 주문 취소한 코인개수(NextCandle 및 Kill_Confirm에서 사용)
    sideCanceled = ""           # 취소한 주문의 방향("buy" or "sell")
    isSendSignalDiscord = False # 트뷰 시그널이 도착했다는 알람 전송 여부

    for nTry in range(nMaxTry):
        if nGoal != 0 and nComplete == nGoal:   # 이미 매매를 성공하면 더이상의 Try를 생략함.
            break

        try:
            if order_info.order_name in HatikoInfo.nearSignal_list:
                # near 시그널 처리
                # 예시) nearLong1 시그널 수신
                # nearLong1_dic 최대개수 확인 -> 미달 시, 지정가 매수주문 -> 성공 시, nearLong1_dic에 추가

                # 0. 먼저 발생하는 시그널 무시
                near_ignore_list = hatikoInfo.matchNearIgnoreList(order_info.order_name)
                if (order_info.side == "buy" and len(near_ignore_list) < hatikoInfo.nIgnoreLong) or \
                    (order_info.side == "sell" and len(near_ignore_list) < hatikoInfo.nIgnoreShort):
                    if order_info.base not in near_ignore_list:
                        near_ignore_list.append(order_info.base)
                    # background_tasks.add_task(log_custom_message, order_info, "IGNORE") if USE_DISCORD else None
                    log_custom_message(order_info, "IGNORE") if USE_DISCORD else None
                    return {"result" : "ignore"}

                # 1. 종목 최대개수 확인
                near_dic = hatikoInfo.matchNearDic(order_info.order_name)
                if order_info.side == "buy" and (len(near_dic) >= hatikoInfo.nMaxLong or order_info.base in near_dic):
                    return {"result" : "ignore"}
                if order_info.side == "sell" and (len(near_dic) >= hatikoInfo.nMaxShort or order_info.base in near_dic):
                    return {"result" : "ignore"}
                
                # 2. 거래소 객체 생성
                exchange_name = order_info.exchange
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)

                # 3. 지정가 Entry 주문
                if bot.order_info.is_entry or (bot.order_info.is_spot and bot.order_info.is_buy):
                    ###################################
                    # Entry 매매 코드
                    ###################################

                    if not isSettingFinish:   # 초기 세팅
                        symbol = order_info.unified_symbol
                        if order_info.is_futures and order_info.leverage is not None: 
                            bot.set_leverage(order_info.leverage, symbol)

                        # 진입수량 설정
                        entryRate = hatikoInfo.calcEntryRate(hatikoInfo.nMaxLong, safetyMarginPercent=1) if order_info.is_spot else 0 # entryCash / FreeCash  # 현물에서 사용
                        log_message(f"entryRate : {entryRate}") if LOG else None
                        total_amount = bot.get_amount_hatiko(symbol, hatikoInfo.nMaxLong, hatikoInfo.nMaxShort, entryRate)
                        log_message(f"total_amount : {total_amount}") if LOG else None
                        max_amount, min_amount = getMinMaxQty(bot, order_info)
                        log_message(f"max_amount : {max_amount}, min_amount : {min_amount}") if LOG else None

                        # Set nGoal and entry_amount_list
                        if total_amount % max_amount < min_amount:
                            nGoal = total_amount // max_amount
                            entry_amount_list = [max_amount] * int(nGoal)
                        else:
                            nGoal = total_amount // max_amount + 1
                            entry_amount_list = [max_amount] * int(nGoal - 1)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            log_message(f"remain_amount : {remain_amount}") if LOG else None
                            entry_amount_list.append(remain_amount)
                        
                        # 진입 가격은 order_info로 넘겨받음
                        entry_price = order_info.price  
                        isSettingFinish = True
                    
                    # 매매 주문
                    log_message(f"nGoal : {nGoal}") if LOG else None
                    for i in range(int(nGoal - nComplete)):
                        entry_amount = entry_amount_list[nComplete]
                        # order_result = bot.client.create_order(symbol, "limit", side, abs(entry_amount), entry_price)
                        log_message(f"entry_amount {i} : {entry_amount}") if LOG else None
                        order_result = bot.limit_order(order_info, entry_amount, entry_price)   # 실패 시 재시도는 bot.limit_order 안에서 처리
                        orderID_list.append(order_result["id"])
                        nComplete += 1
                        # 디스코드 로그생성
                        updateOrderInfo(order_info, amount=entry_amount)
                        if order_info.is_spot:
                            order_info.leverage = None
                        # background_tasks.add_task(log, exchange_name, order_result, order_info) if USE_DISCORD else None
                        log(exchange_name, order_result, order_info) if USE_DISCORD else None
                    
                # 4. 매매가 전부 종료되면 near딕셔너리 업데이트
                near_dic[order_info.base] = orderID_list
                log_message(f"len(orderID_list) : {len(orderID_list)}") if LOG else None

            elif order_info.order_name in HatikoInfo.entrySignal_list:
                # Long or Short 시그널 처리
                # 예시) Long1 시그널 수신
                # 해당 종목이 nearLong1_dic에 존재하는지 확인 -> 존재 시, Long1 리스트에 추가
                
                near_dic = hatikoInfo.matchNearDic(order_info.order_name)
                entry_list = hatikoInfo.matchEntryList(order_info.order_name)
                if order_info.base in near_dic and order_info.base not in entry_list:
                    entry_list.append(order_info.base)
                    # [Debug] 트뷰 시그널이 도착했다는 알람 발생
                    if not isSendSignalDiscord:
                        # background_tasks.add_task(log_custom_message, order_info, "ENTRY_SIGNAL")
                        log_custom_message(order_info, "ENTRY_SIGNAL")
                        isSendSignalDiscord = True

            elif order_info.order_name in HatikoInfo.nextSignal_list:
                # NextCandle 시그널 처리
                # 예시) NextCandle_L1 시그널 수신
                # 해당 종목이 nearLong1_dic에 존재하는지 확인 -> 존재 시, Long1_list에 없으면 미체결주문 체크 -> 미체결주문 취소 & 신규 Long1 주문
                
                # 0. 트뷰에서는 청산 시그널로 오기 때문에 진입으로 order_info 수정 후 디스코드 알람 전송
                if order_info.is_futures and order_info.is_close:
                    order_info.is_entry = True
                    order_info.is_close = None
                    order_info.is_buy = None if order_info.is_buy else True
                    order_info.is_sell = None if order_info.is_sell else True
                if order_info.is_spot:
                    order_info.is_buy = None
                    order_info.is_sell = True

                # 1. 봉마감 후 재주문이 필요없으면 무시
                near_dic = hatikoInfo.matchNearDic(order_info.order_name)
                entry_list = hatikoInfo.matchEntryList(order_info.order_name)
                if order_info.base not in near_dic or order_info.base in entry_list: 
                    return {"result" : "ignore"}

                # 2. 미체결 주문 취소 & 재주문
                exchange_name = order_info.exchange
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol

                orderID_list_old = near_dic[order_info.base]
                log_message(f"len(orderID_list_old): {len(orderID_list_old)}") if LOG else None
                for orderID in orderID_list_old:
                    log_message(f"orderID : {orderID}") if LOG else None
                    # 미체결 주문 취소
                    order = bot.client.fetch_order(orderID, symbol)
                    log_message(f"order['status'] : {order['status']}") if LOG else None
                    if order["status"] == "canceled":
                        isCancelSuccess = True
                        amountCanceled = order["remaining"]
                        sideCanceled = order["side"]
                    elif order["status"] == "open":
                        isReEntry = True
                        resultCancel = bot.client.cancel_order(orderID, symbol)
                        log_message(f"resultCancel['status'] : {resultCancel['status']}") if LOG else None
                        time.sleep(0.1) # 비트겟은 취소 후 바로 orderStatus를 조회하면 취소가 안된 상태로 조회됨
                        orderAfterCancel = bot.client.fetch_order(orderID, symbol)
                        log_message(f"orderAfterCancel['status'] : {orderAfterCancel['status']}") if LOG else None
                        if orderAfterCancel["status"] == "canceled":
                            isCancelSuccess = True
                            amountCanceled = orderAfterCancel["remaining"]
                            sideCanceled = orderAfterCancel["side"]
                            # [Debug] 미체결 주문 취소 후 알람 발생
                            # background_tasks.add_task(log_custom_message, order_info, "CANCEL_ORDER") if USE_DISCORD else None
                            log_custom_message(order_info, "CANCEL_ORDER") if USE_DISCORD else None

                    if isReEntry:
                        # 재주문 
                        log_message(f"symbol : {symbol}, sideCanceled : {sideCanceled}, amountCanceled : {amountCanceled}, price : {order_info.price}") if LOG else None
                        order_result = bot.client.create_order(symbol, "limit", sideCanceled, amountCanceled, order_info.price)
                        # order_result = bot.limit_order(order_info, amountCanceled, order_info.price)
                        isReEntry = False
                        orderID_list.append(order_result["id"])

                        updateOrderInfo(order_info, amount=amountCanceled, side=sideCanceled)
                        # background_tasks.add_task(log, exchange_name, order_result, order_info) if USE_DISCORD else None
                        log(exchange_name, order_result, order_info) if USE_DISCORD else None

                # 3. near_dic 오더id 업데이트
                if isCancelSuccess:
                    near_dic[order_info.base] = orderID_list
                else:
                    # 트뷰로부터 특정 시그널 손실로 인해 이미 체결된 주문인 경우
                    entry_list.append(order_info.base)
                    # background_tasks.add_task(log_custom_message, order_info, "ORDER_CLOSED")
                    log_custom_message(order_info, "ORDER_CLOSED")
                    return {"result" : "ignore"}

            elif order_info.order_name in HatikoInfo.nextCloseSignal_list:
                # NextCandle Close 시그널 처리
                # 예시) NextCandle_LF 시그널 수신
                # 해당 종목이 Entry_list에 존재하는지 확인 -> 미체결 청산주문 체크 -> 미체결 주문 취소 후 모든 보유수량으로 청산주문 -> orderId_list에 추가
                
                # 1. Entry_list에 존재하는지 확인
                if order_info.order_name == "NextCandle_LF" and order_info.base not in (hatikoInfo.Long1_list + hatikoInfo.Long2_list + hatikoInfo.Long3_list + hatikoInfo.Long4_list):
                    return {"result" : "ignore"}
                if order_info.order_name == "NextCandle_SF" and order_info.base not in (hatikoInfo.Short1_list + hatikoInfo.Short2_list + hatikoInfo.Short3_list + hatikoInfo.Short4_list):
                    return {"result" : "ignore"}

                # 2. 미체결 청산주문 취소
                exchange_name = order_info.exchange
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol
                open_orders = bot.client.fetch_open_orders(symbol)
                for open_order in open_orders:
                    if (open_order["side"] == "sell" and order_info.order_name == "NextCandle_LF") or (open_order["side"] == "buy" and order_info.order_name == "NextCandle_SF"):
                        bot.client.cancel_order(open_order["id"], symbol)
                        isCancelSuccess = True

                # 미체결 주문 취소 후 알람 발생
                if not isSendSignalDiscord and isCancelSuccess:
                    # background_tasks.add_task(log_custom_message, order_info, "CANCEL_ORDER") if USE_DISCORD else None
                    log_custom_message(order_info, "CANCEL_ORDER") if USE_DISCORD else None
                    isSendSignalDiscord = True

                # 3. 모든 보유수량으로 청산주문
                if order_info.is_close or (bot.order_info.is_spot and bot.order_info.is_sell):
                    #############################
                    ## Close 매매코드
                    #############################
                    if not isSettingFinish:
                        # 초기 세팅
                        # total amount를 max_amount로 쪼개기
                        total_amount = bot.get_amount_hatiko(symbol, hatikoInfo.nMaxLong, hatikoInfo.nMaxShort)
                        log_message(f"total_amount : {total_amount}") if LOG else None
                        max_amount, min_amount = getMinMaxQty(bot, order_info)
                        log_message(f"max_amount : {max_amount}, min_amount : {min_amount}") if LOG else None

                        # Set nGoal and entry_amount_list
                        if total_amount % max_amount < min_amount:
                            nGoal = total_amount // max_amount
                            close_amount_list = [max_amount] * int(nGoal)
                        else:
                            nGoal = total_amount // max_amount + 1
                            close_amount_list = [max_amount] * int(nGoal - 1)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            log_message(f"remain_amount : {remain_amount}") if LOG else None
                            close_amount_list.append(remain_amount)
                        log_message(f"len(close_amount_list) : {len(close_amount_list)}") if LOG else None
                        
                        # 트뷰에 나오는 청산 가격에 그대로 청산
                        close_price = order_info.price  
                        isSettingFinish = True

                    # (2) 청산 주문
                    for i in range(int(nGoal-nComplete)):
                        close_amount = close_amount_list[nComplete]
                        if close_amount < min_amount:
                            nComplete += 1
                        else:
                            # order_result = bot.future.create_order(symbol, "limit", side, close_amount, close_price, params={"reduceOnly": True})
                            log_message(f"close_amount : {close_amount}") if LOG else None
                            order_result = bot.limit_order(order_info, close_amount, close_price)
                            orderID_list.append(order_result["id"])
                            nComplete += 1
                            updateOrderInfo(order_info, amount=close_amount)
                            # background_tasks.add_task(log, exchange_name, order_result, order_info) if USE_DISCORD else None
                            log(exchange_name, order_result, order_info) if USE_DISCORD else None

                    # 4. 매매가 전부 종료되면 closePrice_dic 업데이트
                    hatikoInfo.closePrice_dic[order_info.base] = close_price

            elif order_info.order_name in HatikoInfo.closeSignal_list:
                # 청산 시그널 처리
                # 예시) 청산 시그널 수신
                # 해당 종목이 nearLong1_list에 존재하는지 확인 -> 존재 시, 청산 주문 & 미체결 주문 취소 -> 성공 시, 존재하는 모든 리스트에서 제거
                
                # 0. near_ignore_list 초기화
                if removeItemFromMultipleLists(order_info.base,
                                               hatikoInfo.nearLong1_ignore_list, hatikoInfo.nearLong2_ignore_list, hatikoInfo.nearLong3_ignore_list, hatikoInfo.nearLong4_ignore_list,
                                               hatikoInfo.nearShort1_ignore_list, hatikoInfo.nearShort2_ignore_list, hatikoInfo.nearShort3_ignore_list, hatikoInfo.nearShort4_ignore_list):
                    # background_tasks.add_task(log_custom_message, order_info, "IGNORE_CANCEL") if USE_DISCORD else None
                    log_custom_message(order_info, "IGNORE_CANCEL") if USE_DISCORD else None

                # 1. 안 산 주문에 대한 종료 무시
                if order_info.base not in (list(hatikoInfo.nearLong1_dic) + list(hatikoInfo.nearLong2_dic) + list(hatikoInfo.nearLong3_dic) + list(hatikoInfo.nearLong4_dic) + \
                                            list(hatikoInfo.nearShort1_dic) + list(hatikoInfo.nearShort2_dic) + list(hatikoInfo.nearShort3_dic) + list(hatikoInfo.nearShort4_dic)):
                    return {"result" : "ignore"}

                # 2. 미체결 주문 취소
                exchange_name = order_info.exchange
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol
                open_orders = bot.client.fetch_open_orders(symbol)
                for open_order in open_orders:
                    # 미리 청산한 주문인 경우
                    if (open_order["side"] == "sell" and order_info.is_sell) or (open_order["side"] == "buy" and order_info.is_buy):
                        # NextCandle_LF가 씹힌 경우 미체결 주문 취소
                        if (order_info.price != hatikoInfo.closePrice_dic.get(order_info.base)):
                            bot.client.cancel_order(open_order["id"], symbol)
                            isCancelSuccess = True
                    else: # 기존 매수주문 취소
                        bot.client.cancel_order(open_order["id"], symbol)
                        isCancelSuccess = True

                # 미체결 주문 취소 후 알람 발생
                if not isSendSignalDiscord and isCancelSuccess:
                    # background_tasks.add_task(log_custom_message, order_info, "CANCEL_ORDER") if USE_DISCORD else None
                    log_custom_message(order_info, "CANCEL_ORDER") if USE_DISCORD else None
                    isSendSignalDiscord = True

                # 3. 청산 주문
                if order_info.is_close or (bot.order_info.is_spot and bot.order_info.is_sell):
                    #############################
                    ## Close 매매코드
                    #############################
                    if not isSettingFinish:
                        # 초기 세팅
                        # total amount를 max_amount로 쪼개기
                        total_amount = bot.get_amount_hatiko(symbol, hatikoInfo.nMaxLong, hatikoInfo.nMaxShort)
                        log_message(f"total_amount : {total_amount}") if LOG else None
                        max_amount, min_amount = getMinMaxQty(bot, order_info)
                        log_message(f"max_amount : {max_amount}, min_amount : {min_amount}") if LOG else None

                        # Set nGoal and entry_amount_list
                        if total_amount % max_amount < min_amount:
                            nGoal = total_amount // max_amount
                            close_amount_list = [max_amount] * int(nGoal)
                        else:
                            nGoal = total_amount // max_amount + 1
                            close_amount_list = [max_amount] * int(nGoal - 1)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            log_message(f"remain_amount : {remain_amount}") if LOG else None
                            close_amount_list.append(remain_amount)
                        log_message(f"len(close_amount_list) : {len(close_amount_list)}") if LOG else None
                        
                        # 트뷰에 나오는 청산 가격에 그대로 청산
                        close_price = order_info.price  
                        isSettingFinish = True

                    # (2) 청산 주문
                    for i in range(int(nGoal-nComplete)):
                        close_amount = close_amount_list[nComplete]
                        if close_amount < min_amount:
                            nComplete += 1
                        else:
                            # order_result = bot.future.create_order(symbol, "limit", side, close_amount, close_price, params={"reduceOnly": True})
                            log_message(f"close_amount : {close_amount}") if LOG else None
                            order_result = bot.limit_order(order_info, close_amount, close_price)
                            isOrderSuccess = True
                            nComplete += 1
                            updateOrderInfo(order_info, amount=close_amount)
                            # background_tasks.add_task(log, exchange_name, order_result, order_info)
                            log(exchange_name, order_result, order_info)

                # 미체결 주문 취소한 것도 없고, 새로 청산주문할 것도 없는 경우 알람 발생
                if not isSendSignalDiscord and not isCancelSuccess and not isOrderSuccess:
                    # background_tasks.add_task(log_custom_message, order_info, "CLOSE_ORDER")
                    log_custom_message(order_info, "CLOSE_SIGNAL")
                    isSendSignalDiscord = True

                # 4. 매매가 전부 종료된 후 매매종목 리스트 업데이트
                removeItemFromMultipleDicts(order_info.base,
                                            hatikoInfo.nearLong1_dic, hatikoInfo.nearLong2_dic, hatikoInfo.nearLong3_dic, hatikoInfo.nearLong4_dic,
                                            hatikoInfo.nearShort1_dic, hatikoInfo.nearShort2_dic, hatikoInfo.nearShort3_dic, hatikoInfo.nearShort4_dic,
                                            hatikoInfo.closePrice_dic)
                removeItemFromMultipleLists(order_info.base,
                                            hatikoInfo.Long1_list, hatikoInfo.Long2_list, hatikoInfo.Long3_list, hatikoInfo.Long4_list,
                                            hatikoInfo.Short1_list, hatikoInfo.Short2_list, hatikoInfo.Short3_list, hatikoInfo.Short4_list)

                # 5. 시간차 청산주문 스레드 생성 (5분 후 미체결 건은 시장가 청산)
                if KILL_CONFIRM:
                    kill_confirm_thread = threading.Thread(target=kill_confirm_thread_func, args=(order_info,))
                    kill_confirm_thread.start()

            elif order_info.order_name in HatikoInfo.ignoreSignal_list:
                return {"result" : "ignore"}

            elif order_info.order_name == "Kill_Confirm":
                # 1. 미체결 주문 취소
                exchange_name = order_info.exchange
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol
                open_orders = bot.client.fetch_open_orders(symbol)
                for open_order in open_orders :
                    # 청산 주문이 남아있는 경우
                    if (open_order["side"] == "sell" and order_info.is_sell) or (open_order["side"] == "buy" and order_info.is_buy) :
                        bot.client.cancel_order(open_order["id"], symbol)
                        amountCanceled += open_order["remaining"]
                        isCancelSuccess = True
                            
                # 2. 청산 주문
                if order_info.is_close or (bot.order_info.is_spot and bot.order_info.is_sell) :
                    #############################
                    ## Close 매매코드
                    #############################
                    if not isSettingFinish :
                        # 초기 세팅
                        # total amount를 max_amount로 쪼개기
                        total_amount = amountCanceled
                        log_message(f"total_amount : {total_amount}") if LOG else None
                        max_amount, min_amount = getMinMaxQty(bot, order_info)
                        log_message(f"max_amount : {max_amount}, min_amount : {min_amount}") if LOG else None

                        # Set nGoal and entry_amount_list
                        if total_amount% max_amount < min_amount:
                            nGoal = total_amount // max_amount
                            close_amount_list = [max_amount] * int(nGoal)
                        else:
                            nGoal = total_amount // max_amount + 1
                            close_amount_list = [max_amount] * int(nGoal - 1)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            log_message(f"remain_amount : {remain_amount}") if LOG else None
                            close_amount_list.append(remain_amount)
                            log_message(f"len(close_amount_list) : {len(close_amount_list)}") if LOG else None

                    # 청산가는 현재가 * 0.95
                    close_price = bot.get_price(symbol) * 0.95
                    isSettingFinish = True

                    # 청산 주문
                    for i in range(int(nGoal - nComplete)) :
                        close_amount = close_amount_list[nComplete]
                        if close_amount < min_amount :
                            nComplete += 1
                        else :
                            # order_result = bot.future.create_order(symbol, "limit", side, close_amount, close_price, params = { "reduceOnly": True })
                            log_message(f"close_amount : {close_amount}") if LOG else None
                            order_result = bot.limit_order(order_info, close_amount, close_price)
                            isOrderSuccess = True
                            nComplete += 1
                            updateOrderInfo(order_info, amount = close_amount)
                            log(exchange_name, order_result, order_info)

            else:
                # background_tasks.add_task(log_custom_message, order_info, "ORDER_NAME_INCORRECT")
                log_custom_message(order_info, "ORDER_NAME_INCORRECT")
                return {"result" : "ignore"}

        except TypeError as e:
            error_msg = get_error(e)
            # background_tasks.add_task(log_order_error_message, "\n".join(error_msg), order_info)
            log_order_error_message("\n".join(error_msg), order_info)

        except Exception as e:
            error_msg = get_error(e)
            # background_tasks.add_task(log_error, "\n".join(error_msg), order_info)
            log_error("\n".join(error_msg), order_info)

        else:
            return {"result": "success"}

        finally:
            pass


#endregion Hatiko Main Function



#endregion ############################### Hatiko ###############################


#region ############################### 켈트너 + Hatiko in Upbit #################################

kctrend_long_list = []   # 켈트너 추세 전략 Long 진입 중인 종목들 list

hatiko_long1_list = []    # Hatiko 전략 Long1 진입 중인 종목들 list
hatiko_long2_list = []    # Hatiko 전략 Long2 진입 중인 종목들 list
hatiko_long3_list = []    # Hatiko 전략 Long3 진입 중인 종목들 list
hatiko_long4_list = []    # Hatiko 전략 Long4 진입 중인 종목들 list

@ app.get("/reset_kctrendandhatiko")
async def resetkctrendandhatiko():
    global kctrend_long_list, hatiko_long1_list, hatiko_long2_list, hatiko_long3_list, hatiko_long4_list

    # kctrend + hatiko 관련 전역변수 초기화
    kctrend_long_list = []

    hatiko_long1_list = []
    hatiko_long2_list = []
    hatiko_long3_list = []
    hatiko_long4_list = []
    
    return "Intialize kctrend & hatiko Variables Completed!!!"

@ app.get("/kctrendandhatiko_info")
async def kctrendandhatikoinfo():
    res = {
        "kctrend_long_list" : str(kctrend_long_list),
        "hatiko_long1_list" : str(hatiko_long1_list),
        "hatiko_long2_list" : str(hatiko_long2_list),
        "hatiko_long3_list" : str(hatiko_long3_list),
        "hatiko_long4_list" : str(hatiko_long4_list),
    }
    return res

def match_hatiko_long_list(order_name: str):
    """
    [켈트너 + Hatiko 결합 전략 전용 함수]
    order_name에 따라 해당하는 hatiko_long_list를 반환
    """
    global hatiko_long1_list, hatiko_long2_list, hatiko_long3_list, hatiko_long4_list

    if order_name in ["nearLong1", "Long1", "NextCandle_L1"]:
        return hatiko_long1_list
    if order_name in ["nearLong2", "Long2", "NextCandle_L2"]:
        return hatiko_long2_list
    if order_name in ["nearLong3", "Long3", "NextCandle_L3"]:
        return hatiko_long3_list
    if order_name in ["nearLong4", "Long4", "NextCandle_L4"]:
        return hatiko_long4_list

def get_amount_kctrend_hatiko(order_info: MarketOrder, bot):
    global kctrend_long_list, hatiko_long1_list, hatiko_long2_list, hatiko_long3_list, hatiko_long4_list

    kctrend_buy_signal_list = ["kctrend Long"]
    hatiko_buy_signal_list = ["Long1", "Long2", "Long3", "Long4"]

    # upbit 계좌 상태 읽어오기
    try :
        free_cash = bot.get_balance(order_info.quote)
    except:
        free_cash = 0.0

    try:
        free_coin = bot.get_balance(order_info.base)
    except:
        free_coin = 0.0

    # 진입 오더의 경우
    if order_info.is_spot and order_info.is_buy:
        # hatiko 진입 갯수 확인
        hatiko_count_all = len(hatiko_long1_list + hatiko_long2_list + hatiko_long3_list + hatiko_long4_list)
        hatiko_count_mine = (hatiko_long1_list + hatiko_long2_list + hatiko_long3_list + hatiko_long4_list).count(order_info.base)
        
        # 켈트너 진입 갯수 확인
        kctrend_count_all = len(kctrend_long_list)

        # total cash 계산
        used_hatiko_portion_all = hatiko_count_all / 8.0
        used_kctrend_portion_all = kctrend_count_all / 2.0
        used_portion = used_hatiko_portion_all + used_kctrend_portion_all
        rest_portion_all = 1 - used_portion
        if rest_portion_all <= 0:
            return 0
        total_cash = free_cash / rest_portion_all

        # ---------- 시그널 종류에 따른 차이 ----------
        # case 1) 켈트너 진입 오더
        if order_info.order_name in kctrend_buy_signal_list:
            # kctrend에 사용할 cash 계산
            cash_for_kctrend = total_cash / 2
            used_cash_in_hatiko_already = total_cash * hatiko_count_mine / 8
            entry_cash = cash_for_kctrend - used_cash_in_hatiko_already
            
        # case 2) Hatiko 진입 오더
        if order_info.order_name in hatiko_buy_signal_list:
            # hatiko 신규 포지션에 사용할 cash 계산
            cash_for_hatiko = total_cash / 8
            entry_cash = cash_for_hatiko
        # ---------------------------------------------

        # entry_cash 보정
        if entry_cash > free_cash - 10000:  # 1만원은 여윳돈
            entry_cash = free_cash - 10000
            entry_cash = 0 if entry_cash < 10000 else entry_cash    # 1만원 보다 작은 포지션은 들어가지 않는다.

        # amount 계산
        entry_price = order_info.price
        entry_amount = entry_cash / entry_price
        return entry_amount
        
    # 청산 오더의 경우
    if order_info.is_spot and order_info.is_sell:
        if free_coin is None:
            return 0
        return free_coin

@ app.post("/kctrendandhatiko")
@ app.post("/")
async def kctrendandhatiko(order_info: MarketOrder, background_tasks: BackgroundTasks):
    """
    켈트너 추세전략과 hatiko를 섞어서 쓰는 전략
    
    거래소 : Upbit
    종목 : 2개 (BTC, ETH)
    로직 : 켈트너가 hatiko 보다 우선권을 가진다.
           켈트너 진입 중일 때 hatiko 시그널은 무시, hatiko 진입 중일 때 켈트너 시그널이 뜨면 hatiko는 거기서 중단하고 켈트너로 편입
    베팅비율 : 총 Balance를 기준으로 각 종목은 50% 씩 할당된다. hatiko 시그널의 경우 50% 안에서 1/4 하여 포지션을 베팅한다.
    """

    global kctrend_long_list, hatiko_long1_list, hatiko_long2_list, hatiko_long3_list, hatiko_long4_list

    # order_name 리스트
    kctrend_buy_signal_list = ["kctrend Long"]
    kctrend_sell_signal_list = ["kctrend Long Close"]
    hatiko_buy_signal_list = ["Long1", "Long2", "Long3", "Long4"]
    hatiko_sell_signal_list = ["close Longs on open", "TakeProfitL1"]
    hatiko_ignore_signal_list = ["TakeProfitL2", "TakeProfitL3", "TakeProfitL4"]

    # order_result 변수 선언
    order_result = None

    try :
        # 1. upbit 객체 생성
        exchange_name = order_info.exchange
        bot = get_bot(exchange_name, order_info.kis_number)
        bot.init_info(order_info)

        # 2. order_name 확인
        order_name = order_info.order_name

        # 2-1. 켈트너 전략 진입 시그널
        if order_name in kctrend_buy_signal_list:
            ## (1) 켈트너 전략 기진입 여부 확인
            if order_info.base in kctrend_long_list:
                return {"result" : "ignore"}
            
            ## (2) 켈트너 전략 진입 (Hatiko 전략의 포지션을 켈트너 전략으로 편입)
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.market_order(order_info)

            ## (3) 켈트너 목록 추가
            if order_info.base not in kctrend_long_list:
                kctrend_long_list.append(order_info.base)

            ## (4) Hatiko 목록 초기화       
            if order_info.base in hatiko_long1_list:
                hatiko_long1_list.remove(order_info.base)
            if order_info.base in hatiko_long2_list:
                hatiko_long2_list.remove(order_info.base)
            if order_info.base in hatiko_long3_list:
                hatiko_long3_list.remove(order_info.base)
            if order_info.base in hatiko_long4_list:
                hatiko_long4_list.remove(order_info.base)
        
        # 2-2. 켈트너 전략 청산 시그널
        elif order_name in kctrend_sell_signal_list:
            ## (1) 켈트너 전략 포지션 확인
            if order_info.base not in kctrend_long_list:
                return {"result" : "ignore"}
            
            ## (2) 켈트너 전략 청산
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.market_order(order_info)

            ## (3) 켈트너 목록 초기화
            if order_info.base in kctrend_long_list:
                kctrend_long_list.remove(order_info.base)
            
        # 2-3. Hatiko 전략 진입 시그널
        elif order_name in hatiko_buy_signal_list:
            ## (1) 켈트너 포지션 있으면 무시
            if order_info.base in kctrend_long_list:
                return {"result" : "ignore"}
            
            ## (2) Hatiko 기진입 여부 확인
            hatiko_long_list = match_hatiko_long_list(order_info.order_name)
            if order_info.base in hatiko_long_list:
                return {"result" : "ignore"}

            ## (3) Hatiko 전략 진입
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.market_order(order_info)

            ## (4) Hatiko 목록 추가
            hatiko_long_list.append(order_info.base)
        
        # 2-4. Hatiko 전략 청산 시그널
        elif order_name in hatiko_sell_signal_list:
            ## (1) 켈트너 포지션 있으면 무시
            if order_info.base in kctrend_long_list:
                return {"result" : "ignore"}

            ## (2) Hatiko 포지션 확인
            if order_info.base not in (hatiko_long1_list + hatiko_long2_list + hatiko_long3_list + hatiko_long4_list):
                return {"result" : "ignore"}
            
            ## (3) Hatiko 전략 청산
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.market_order(order_info)

            ## (4) Hatiko 목록 초기화
            if order_info.base in hatiko_long1_list:
                hatiko_long1_list.remove(order_info.base)
            if order_info.base in hatiko_long2_list:
                hatiko_long2_list.remove(order_info.base)
            if order_info.base in hatiko_long3_list:
                hatiko_long3_list.remove(order_info.base)
            if order_info.base in hatiko_long4_list:
                hatiko_long4_list.remove(order_info.base)
        
        # 2-5. 무시하는 시그널
        elif order_name in hatiko_ignore_signal_list:
            return {"result" : "ignore"}
        # 2-6. 예상 외의 시그널
        else:
            background_tasks.add_task(log_custom_message, order_info, "ORDER_NAME_INCORRECT")
            return {"result" : "ignore"}

        # 3. 디스코드 알람 발생
        if order_result is not None:
            background_tasks.add_task(log, exchange_name, order_result, order_info)
        else:
            background_tasks.add_task(log_custom_message, order_info, "RECV_BUT_NO_ORDER")
            
    except TypeError as e:
        error_msg = get_error(e)
        background_tasks.add_task(log_order_error_message, "\n".join(error_msg), order_info)

    except Exception as e:
        error_msg = get_error(e)
        background_tasks.add_task(log_error, "\n".join(error_msg), order_info)

    else:
        return {"result": "success"}

    finally:
        pass

@ app.post("/kctrendandhatiko_limit")
@ app.post("/")
async def kctrendandhatikolimit(order_info: MarketOrder, background_tasks: BackgroundTasks):
    """
    [지정가 버전 - client.market_order만 client.limit_order로 변경]
    켈트너 추세전략과 hatiko를 섞어서 쓰는 전략
    
    거래소 : Upbit
    종목 : 2개 (BTC, ETH)
    로직 : 켈트너가 hatiko 보다 우선권을 가진다.
           켈트너 진입 중일 때 hatiko 시그널은 무시, hatiko 진입 중일 때 켈트너 시그널이 뜨면 hatiko는 거기서 중단하고 켈트너로 편입
    베팅비율 : 총 Balance를 기준으로 각 종목은 50% 씩 할당된다. hatiko 시그널의 경우 50% 안에서 1/4 하여 포지션을 베팅한다.
    """

    global kctrend_long_list, hatiko_long1_list, hatiko_long2_list, hatiko_long3_list, hatiko_long4_list

    # order_name 리스트
    kctrend_buy_signal_list = ["kctrend Long"]
    kctrend_sell_signal_list = ["kctrend Long Close"]
    hatiko_buy_signal_list = ["Long1", "Long2", "Long3", "Long4"]
    hatiko_sell_signal_list = ["close Longs on open", "TakeProfitL1"]
    hatiko_ignore_signal_list = ["TakeProfitL2", "TakeProfitL3", "TakeProfitL4"]

    # order_result 변수 선언
    order_result = None

    try :
        # 1. upbit 객체 생성
        exchange_name = order_info.exchange
        bot = get_bot(exchange_name, order_info.kis_number)
        bot.init_info(order_info)

        # 2. order_name 확인
        order_name = order_info.order_name

        # 2-1. 켈트너 전략 진입 시그널
        if order_name in kctrend_buy_signal_list:
            ## (1) 켈트너 전략 진입 (Hatiko 전략의 포지션을 켈트너 전략으로 편입)
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.limit_order(order_info)

            ## (2) 켈트너 목록 추가
            if order_info.base not in kctrend_long_list:
                kctrend_long_list.append(order_info.base)

            ## (3) Hatiko 목록 초기화       
            if order_info.base in hatiko_long1_list:
                hatiko_long1_list.remove(order_info.base)
            if order_info.base in hatiko_long2_list:
                hatiko_long2_list.remove(order_info.base)
            if order_info.base in hatiko_long3_list:
                hatiko_long3_list.remove(order_info.base)
            if order_info.base in hatiko_long4_list:
                hatiko_long4_list.remove(order_info.base)
        
        # 2-2. 켈트너 전략 청산 시그널
        elif order_name in kctrend_sell_signal_list:
            ## (1) 켈트너 전략 청산
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.limit_order(order_info)

            ## (2) 켈트너 목록 초기화
            if order_info.base in kctrend_long_list:
                kctrend_long_list.remove(order_info.base)
            
        # 2-3. Hatiko 전략 진입 시그널
        elif order_name in hatiko_buy_signal_list:
            ## (1) 켈트너 포지션 있으면 무시
            if order_info.base in kctrend_long_list:
                return {"result" : "ignore"}
            
            ## (2) Hatiko 기진입 여부 확인
            hatiko_long_list = match_hatiko_long_list(order_info.order_name)
            if order_info.base in hatiko_long_list:
                return {"result" : "ignore"}

            ## (3) Hatiko 전략 진입
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.limit_order(order_info)

            ## (4) Hatiko 목록 추가
            hatiko_long_list.append(order_info.base)
        
        # 2-4. Hatiko 전략 청산 시그널
        elif order_name in hatiko_sell_signal_list:
            ## (1) 켈트너 포지션 있으면 무시
            if order_info.base in kctrend_long_list:
                return {"result" : "ignore"}

            ## (2) Hatiko 포지션 확인
            if order_info.base not in (hatiko_long1_list + hatiko_long2_list + hatiko_long3_list + hatiko_long4_list):
                return {"result" : "ignore"}
            
            ## (3) Hatiko 전략 청산
            order_info.amount = get_amount_kctrend_hatiko(order_info, bot)
            if order_info.amount > 0:
                order_result = bot.limit_order(order_info)

            ## (4) Hatiko 목록 초기화
            if order_info.base in hatiko_long1_list:
                hatiko_long1_list.remove(order_info.base)
            if order_info.base in hatiko_long2_list:
                hatiko_long2_list.remove(order_info.base)
            if order_info.base in hatiko_long3_list:
                hatiko_long3_list.remove(order_info.base)
            if order_info.base in hatiko_long4_list:
                hatiko_long4_list.remove(order_info.base)
        
        # 2-5. 무시하는 시그널
        elif order_name in hatiko_ignore_signal_list:
            return {"result" : "ignore"}
        # 2-6. 예상 외의 시그널
        else:
            background_tasks.add_task(log_custom_message, order_info, "ORDER_NAME_INCORRECT")
            return {"result" : "ignore"}

        # 3. 디스코드 알람 발생
        if order_result is not None:
            background_tasks.add_task(log, exchange_name, order_result, order_info)
        else:
            background_tasks.add_task(log_custom_message, order_info, "RECV_BUT_NO_ORDER")
            
    except TypeError as e:
        error_msg = get_error(e)
        background_tasks.add_task(log_order_error_message, "\n".join(error_msg), order_info)

    except Exception as e:
        error_msg = get_error(e)
        background_tasks.add_task(log_error, "\n".join(error_msg), order_info)

    else:
        return {"result": "success"}

    finally:
        pass
#endregion ############################### 켈트너 + Hatiko in Upbit #################################


#region ############################### [Hedge 변형] 선물 간 차익거래 #################################

def get_arbi_records(base: str, exchange_name_long: str, exchange_name_short: str):
    records = pocket.get_full_list("arbitrage", query_params={"filter": f'base = "{base}"'})
    short_amount = 0.0
    short_records_id = []
    long_amount = 0.0
    long_records_id = []
    for record in records:
        if record.exchange == exchange_name_short:
            short_amount += record.amount
            short_records_id.append(record.id)
        elif record.exchange == exchange_name_long:
            long_amount += record.amount
            long_records_id.append(record.id)

    return {
        exchange_name_short: {"amount": short_amount, "records_id": short_records_id},
        exchange_name_long: {"amount": long_amount, "records_id": long_records_id},
    }

@app.post("/arbitrage")
async def arbitrage(arbi_data: ArbiData, background_tasks: BackgroundTasks):
    exchange_name_long = arbi_data.exchange_long.upper()
    exchange_name_short = arbi_data.exchange_short.upper()
    bot_long = get_bot(exchange_name_long) #upbit = get_bot("UPBIT")
    bot_short = get_bot(exchange_name_short) #bot = get_bot(exchange_name)
    
    base = arbi_data.base
    quote = arbi_data.quote
    amount = arbi_data.amount
    leverage = arbi_data.leverage
    hedge = arbi_data.hedge

    short_order_info = OrderRequest(
        exchange=exchange_name_short,
        base=base,
        quote=quote,
        side="entry/sell",
        type="market",
        amount=amount,
        leverage=leverage,
    )
    bot_short.init_info(short_order_info)

    if hedge == "ON":
        try:
            if amount is None:
                raise Exception("헷지할 수량을 요청하세요")
            short_order_result = bot_short.market_entry(short_order_info)
            short_order_amount = short_order_result["amount"]
            
            pocket.create("arbitrage", {"exchange": exchange_name_short, "base": base, "quote": quote, "amount": short_order_amount})
            if leverage is None:
                leverage = 1
            try:
                long_order_info = OrderRequest(
                    exchange=exchange_name_long,
                    base=base,
                    quote=quote,
                    side="entry/buy",
                    type="market",
                    amount=short_order_amount,
                )
                bot_long.init_info(long_order_info)
                long_order_result = bot_long.market_entry(long_order_info)
            except Exception as e:
                hedge_records = get_arbi_records(base, exchange_name_long, exchange_name_short)
                short_records_id = hedge_records[exchange_name_short]["records_id"]
                short_amount = hedge_records[exchange_name_short]["amount"]
                short_order_result = bot_short.market_close(
                    OrderRequest(
                        exchange=exchange_name_short, base=base, quote=quote, side="close/buy", amount=short_amount
                    )
                )
                for short_record_id in short_records_id:
                    pocket.delete("arbitrage", short_record_id)
                log_message("[헷지 실패] Long에서 에러가 발생하여 이에 상응하는 Short 포지션을 종료합니다")
            else:
                # upbit_order_info = bot_long.get_order(long_order_result["id"])
                # upbit_order_amount = upbit_order_info["filled"]
                long_order_amount = long_order_result["amount"]
                pocket.create("arbitrage", {"exchange": exchange_name_long, "base": base, "quote": quote, "amount": long_order_amount})
                log_arbi_message(exchange_name_long, exchange_name_short, base, quote, long_order_amount, short_order_amount, hedge)

        except Exception as e:
            # log_message(f"{e}")
            background_tasks.add_task(log_error_message, traceback.format_exc(), "헷지 에러")
            return {"result": "error"}
        else:
            return {"result": "success"}

    elif hedge == "OFF":
        try:
            records = pocket.get_full_list("arbitrage", query_params={"filter": f'base = "{base}"'})
            short_amount = 0.0
            short_records_id = []
            long_amount = 0.0
            long_records_id = []
            for record in records:
                if record.exchange == exchange_name_short:
                    short_amount += record.amount
                    short_records_id.append(record.id)
                elif record.exchange == exchange_name_long:
                    long_amount += record.amount
                    long_records_id.append(record.id)

            if short_amount > 0 and long_amount > 0:
                # 숏 종료
                order_info = OrderRequest(exchange=exchange_name_short, base=base, quote=quote, side="close/buy", amount=short_amount)
                short_order_result = bot_short.market_close(order_info)
                for short_record_id in short_records_id:
                    pocket.delete("arbitrage", short_record_id)
                # 롱 종료
                order_info = OrderRequest(exchange=exchange_name_long, base=base, quote=quote, side="close/sell", amount=long_amount)
                long_order_result = bot_long.market_close(order_info)
                for long_record_id in long_records_id:
                    pocket.delete("arbitrage", long_record_id)

                log_arbi_message(exchange_name_long, exchange_name_short, base, quote, long_amount, short_amount, hedge)
            elif short_amount == 0 and long_amount == 0:
                log_message(f"{exchange_name_short}, {exchange_name_long}에 종료할 수량이 없습니다")
            elif short_amount == 0:
                log_message(f"{exchange_name_short}에 종료할 수량이 없습니다")
            elif long_amount == 0:
                log_message(f"{exchange_name_long}에 종료할 수량이 없습니다")
        except Exception as e:
            background_tasks.add_task(log_error_message, traceback.format_exc(), "헷지종료 에러")
            return {"result": "error"}
        else:
            return {"result": "success"}
#endregion ############################### [Hedge 변형] 선물 간 차익거래 #################################
