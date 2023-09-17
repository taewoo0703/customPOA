from fastapi.exception_handlers import (
    request_validation_exception_handler,
)
from pprint import pprint
from fastapi import FastAPI, Request, status, BackgroundTasks
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
import httpx
from exchange.stock.kis import KoreaInvestment
from exchange.model import MarketOrder, PriceRequest, HedgeData, OrderRequest, ArbiData, HatikoInfo, HatikoOrder
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

VERSION = "POA : 0.1.1, Hatiko : 230914"
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

# 유효성 검증 후의 order_info 보기 
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


#region ############################### Hatiko ###############################

#region Old Hatiko Logic
# 지정가 Hatiko용 near시그널 딕셔너리
# base(종목명) : orderID_list(오더id 리스트)
nearLong1_dic = {}
nearLong2_dic = {}
nearLong3_dic = {}
nearLong4_dic = {}
nearShort1_dic = {}
nearShort2_dic = {}
nearShort3_dic = {}
nearShort4_dic = {}

# 지정가 Hatiko용 entry시그널 리스트
Long1_list = []
Long2_list = []
Long3_list = []
Long4_list = []
Short1_list = []
Short2_list = []
Short3_list = []
Short4_list = []

# 지정가 Hatiko용 무시할 시그널 리스트
nearLong1_ignore_list = []
nearLong2_ignore_list = []
nearLong3_ignore_list = []
nearLong4_ignore_list = []
nearShort1_ignore_list = []
nearShort2_ignore_list = []
nearShort3_ignore_list = []
nearShort4_ignore_list = []

# Hatiko 봇 에러 시 재진입 횟수
nMaxTry = 5

def matchNearDic(order_name):
    """
    order_name에 따라 해당하는 near딕셔너리를 반환
    예시) input : "NextCandle_L1" -> output : "nearLong1_dic"
    """
    global nearLong1_dic, nearLong2_dic, nearLong3_dic, nearLong4_dic, nearShort1_dic, nearShort2_dic, nearShort3_dic, nearShort4_dic

    if order_name in ["nearLong1", "Long1", "NextCandle_L1"]:
        return nearLong1_dic
    if order_name in ["nearLong2", "Long2", "NextCandle_L2"]:
        return nearLong2_dic
    if order_name in ["nearLong3", "Long3", "NextCandle_L3"]:
        return nearLong3_dic
    if order_name in ["nearLong4", "Long4", "NextCandle_L4"]:
        return nearLong4_dic
    if order_name in ["nearShort1", "Short1", "NextCandle_S1"]:
        return nearShort1_dic
    if order_name in ["nearShort2", "Short2", "NextCandle_S2"]:
        return nearShort2_dic
    if order_name in ["nearShort3", "Short3", "NextCandle_S3"]:
        return nearShort3_dic
    if order_name in ["nearShort4", "Short4", "NextCandle_S4"]:
        return nearShort4_dic

def matchEntryList(order_name):
    """
    order_name에 따라 해당하는 entry리스트를 반환
    예시) input : "NextCandle_L1" -> output : "Long1"
    """
    global Long1_list, Long2_list, Long3_list, Long4_list, Short1_list, Short2_list, Short3_list, Short4_list

    if order_name in ["nearLong1", "Long1", "NextCandle_L1"]:
        return Long1_list
    if order_name in ["nearLong2", "Long2", "NextCandle_L2"]:
        return Long2_list
    if order_name in ["nearLong3", "Long3", "NextCandle_L3"]:
        return Long3_list
    if order_name in ["nearLong4", "Long4", "NextCandle_L4"]:
        return Long4_list
    if order_name in ["nearShort1", "Short1", "NextCandle_S1"]:
        return Short1_list
    if order_name in ["nearShort2", "Short2", "NextCandle_S2"]:
        return Short2_list
    if order_name in ["nearShort3", "Short3", "NextCandle_S3"]:
        return Short3_list
    if order_name in ["nearShort4", "Short4", "NextCandle_S4"]:
        return Short4_list
    
def matchNearIgnoreList(order_name):
    """
    order_name에 따라 해당하는 near_ignore리스트를 반환
    예시) input : "NextCandle_L1" -> output : "nearLong1_ignore_list"
    """
    global nearLong1_ignore_list, nearLong2_ignore_list, nearLong3_ignore_list, nearLong4_ignore_list
    global nearShort1_ignore_list, nearShort2_ignore_list, nearShort3_ignore_list, nearShort4_ignore_list

    if order_name in ["nearLong1", "Long1", "NextCandle_L1"]:
        return nearLong1_ignore_list
    if order_name in ["nearLong2", "Long2", "NextCandle_L2"]:
        return nearLong2_ignore_list
    if order_name in ["nearLong3", "Long3", "NextCandle_L3"]:
        return nearLong3_ignore_list
    if order_name in ["nearLong4", "Long4", "NextCandle_L4"]:
        return nearLong4_ignore_list
    if order_name in ["nearShort1", "Short1", "NextCandle_S1"]:
        return nearShort1_ignore_list
    if order_name in ["nearShort2", "Short2", "NextCandle_S2"]:
        return nearShort2_ignore_list
    if order_name in ["nearShort3", "Short3", "NextCandle_S3"]:
        return nearShort3_ignore_list
    if order_name in ["nearShort4", "Short4", "NextCandle_S4"]:
        return nearShort4_ignore_list

# Hatiko Limit 관련 메모리 모니터
@ app.get("/hatikolimitinfo")
async def hatikolimitinfo():
    res = {
        "nearLong1_dic"  : str(list(nearLong1_dic.keys())),
        "nearLong2_dic"  : str(list(nearLong2_dic.keys())),
        "nearLong3_dic"  : str(list(nearLong3_dic.keys())),
        "nearLong4_dic"  : str(list(nearLong4_dic.keys())),
        "nearShort1_dic" : str(list(nearShort1_dic.keys())),
        "nearShort2_dic" : str(list(nearShort2_dic.keys())),
        "nearShort3_dic" : str(list(nearShort3_dic.keys())),
        "nearShort4_dic" : str(list(nearShort4_dic.keys())),
        "Long1_list"  : str(Long1_list),
        "Long2_list"  : str(Long2_list),
        "Long3_list"  : str(Long3_list),
        "Long4_list"  : str(Long4_list),
        "Short1_list" : str(Short1_list),
        "Short2_list" : str(Short2_list),
        "Short3_list" : str(Short3_list),
        "Short4_list" : str(Short4_list),
        "nearLong1_ignore_list"  : str(nearLong1_ignore_list),
        "nearLong2_ignore_list"  : str(nearLong2_ignore_list),
        "nearLong3_ignore_list"  : str(nearLong3_ignore_list),
        "nearLong4_ignore_list"  : str(nearLong4_ignore_list),
        "nearShort1_ignore_list" : str(nearShort1_ignore_list),
        "nearShort2_ignore_list" : str(nearShort2_ignore_list),
        "nearShort3_ignore_list" : str(nearShort3_ignore_list),
        "nearShort4_ignore_list" : str(nearShort4_ignore_list),
        }
    return res

# Hatiko Limit 관련 전역변수 초기화
@ app.get("/inithatiko")
async def inithatiko():
    global nearLong1_dic, nearLong2_dic, nearLong3_dic, nearLong4_dic
    global nearShort1_dic, nearShort2_dic, nearShort3_dic, nearShort4_dic
    global Long1_list, Long2_list, Long3_list, Long4_list
    global Short1_list, Short2_list, Short3_list, Short4_list
    global nearLong1_ignore_list, nearLong2_ignore_list, nearLong3_ignore_list, nearLong4_ignore_list
    global nearShort1_ignore_list, nearShort2_ignore_list, nearShort3_ignore_list, nearShort4_ignore_list

    nearLong1_dic = {}
    nearLong2_dic = {}
    nearLong3_dic = {}
    nearLong4_dic = {}
    nearShort1_dic = {}
    nearShort2_dic = {}
    nearShort3_dic = {}
    nearShort4_dic = {}

    Long1_list = []
    Long2_list = []
    Long3_list = []
    Long4_list = []
    Short1_list = []
    Short2_list = []
    Short3_list = []
    Short4_list = []

    nearLong1_ignore_list = []
    nearLong2_ignore_list = []
    nearLong3_ignore_list = []
    nearLong4_ignore_list = []
    nearShort1_ignore_list = []
    nearShort2_ignore_list = []
    nearShort3_ignore_list = []
    nearShort4_ignore_list = []

    res = "Intialize Hatiko Variables Completed!!!"
    return res

@ app.post("/hatikolimit")
@ app.post("/")
async def hatikolimit(order_info: MarketOrder, background_tasks: BackgroundTasks):
    nMaxLong = 1
    nMaxShort = 1
    nIgnoreLong = 0
    nIgnoreShort = 0
    hatikolimitBase(order_info, background_tasks, nMaxLong, nMaxShort, nIgnoreLong, nIgnoreShort)

@ app.post("/hatikolimit2")
@ app.post("/")
async def hatikolimit2(order_info: MarketOrder, background_tasks: BackgroundTasks):
    nMaxLong = 2
    nMaxShort = 1
    nIgnoreLong = 0
    nIgnoreShort = 0
    hatikolimitBase(order_info, background_tasks, nMaxLong, nMaxShort, nIgnoreLong, nIgnoreShort)

@ app.post("/hatikolimit4")
@ app.post("/")
async def hatikolimit4(order_info: MarketOrder, background_tasks: BackgroundTasks):
    nMaxLong = 4
    nMaxShort = 1
    nIgnoreLong = 0
    nIgnoreShort = 0
    hatikolimitBase(order_info, background_tasks, nMaxLong, nMaxShort, nIgnoreLong, nIgnoreShort)

@ app.post("/hatikolimit2_ignore1")
@ app.post("/")
async def hatikolimit2_ignore1(order_info: MarketOrder, background_tasks: BackgroundTasks):
    nMaxLong = 2
    nMaxShort = 1
    nIgnoreLong = 1
    nIgnoreShort = 0
    hatikolimitBase(order_info, background_tasks, nMaxLong, nMaxShort, nIgnoreLong, nIgnoreShort)

def hatikolimitBase(order_info: MarketOrder, background_tasks: BackgroundTasks, nMaxLong: int, nMaxShort: int, nIgnoreLong: int, nIgnoreShort: int):
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
    """
    global nearLong1_dic, nearLong2_dic, nearLong3_dic, nearLong4_dic
    global nearShort1_dic, nearShort2_dic, nearShort3_dic, nearShort4_dic
    global Long1_list, Long2_list, Long3_list, Long4_list
    global Short1_list, Short2_list, Short3_list, Short4_list
    global nearLong1_ignore_list, nearLong2_ignore_list, nearLong3_ignore_list, nearLong4_ignore_list
    global nearShort1_ignore_list, nearShort2_ignore_list, nearShort3_ignore_list, nearShort4_ignore_list
    global nMaxTry

    # order_name 리스트
    nearSignal_list = ["nearLong1", "nearLong2", "nearLong3", "nearLong4",
                       "nearShort1", "nearShort2", "nearShort3", "nearShort4"]
    entrySignal_list = ["Long1", "Long2", "Long3", "Long4",
                        "Short1", "Short2", "Short3", "Short4"]
    nextSignal_list = ["NextCandle_L1", "NextCandle_L2", "NextCandle_L3", "NextCandle_L4",
                       "NextCandle_S1", "NextCandle_S2", "NextCandle_S3", "NextCandle_S4"]
    closeSignal_list = ["close Longs on open", "close Shorts on open",
                        "TakeProfit_nearL1", "TakeProfit_nearS1"]
    ignoreSignal_list = ["TakeProfit_nearL2", "TakeProfit_nearL3", "TakeProfit_nearL4",
                         "TakeProfit_nearS2", "TakeProfit_nearS3", "TakeProfit_nearS4",
                         "TakeProfit_L1", "TakeProfit_L2", "TakeProfit_L3", "TakeProfit_L4",
                         "TakeProfit_L1", "TakeProfit_L2", "TakeProfit_L3", "TakeProfit_L4"]

    # 초기화 단계
    order_result = None
    nGoal = 0
    nComplete = 0
    isSettingFinish = False     # 매매전 ccxt 세팅 flag 
    orderID_list = []           # 오더id 리스트
    isCancelSuccess = False     # 미체결주문 취소성공 여부
    amountCanceled = 0          # 주문 취소한 코인개수(NextCandle 로직에서 사용)
    sideCanceled = ""           # 취소한 주문의 방향("buy" or "sell")
    isSendSignalDiscord = False # 트뷰 시그널이 도착했다는 알람 전송 여부

    for nTry in range(nMaxTry):
        if nGoal != 0 and nComplete == nGoal:   # 이미 매매를 성공하면 더이상의 Try를 생략함.
            break

        try:
            if order_info.order_name in nearSignal_list:
                # near 시그널 처리
                # 예시) nearLong1 시그널 수신
                # nearLong1_dic 최대개수 확인 -> 미달 시, 지정가 매수주문 -> 성공 시, nearLong1_dic에 추가

                # 0. 먼저 발생하는 시그널 무시
                near_ignore_list = matchNearIgnoreList(order_info.order_name)
                if (order_info.side == "buy" and len(near_ignore_list) < nIgnoreLong) or \
                    (order_info.side == "sell" and len(near_ignore_list) < nIgnoreShort):
                    if not order_info.base in near_ignore_list:
                        near_ignore_list.append(order_info.base)
                    background_tasks.add_task(log_custom_message, order_info, "IGNORE")
                    return {"result" : "ignore"}

                # 1. 종목 최대개수 확인
                near_dic = matchNearDic(order_info.order_name)
                if order_info.side == "buy" and (len(near_dic) >= nMaxLong or order_info.base in near_dic):
                    return {"result" : "ignore"}
                if order_info.side == "sell" and (len(near_dic) >= nMaxShort or order_info.base in near_dic):
                    return {"result" : "ignore"}
                
                # 2. 거래소 이름 확인
                exchange_name = order_info.exchange
                if exchange_name != "BINANCE":    # Binance Only
                    return {"result" : "ignore"}

                # 3. 지정가 Entry 주문
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)

                if bot.order_info.is_entry:
                    ###################################
                    # Entry 매매 코드
                    ###################################

                    if not isSettingFinish:   # 초기 세팅
                        symbol = order_info.unified_symbol
                        if order_info.leverage is not None: 
                            bot.client.set_leverage(order_info.leverage, symbol)

                        # 진입수량 설정
                        total_amount = bot.get_amount_hatiko(symbol, nMaxLong, nMaxShort)
                        market = bot.client.market(symbol)
                        max_amount = market["limits"]["amount"]["max"] # 지정가 주문 최대 코인개수  # float
                        min_amount = market["limits"]["amount"]["min"] # 지정가 주문 최소 코인개수  # float

                        # Set nGoal
                        entry_amount_list = []
                        if (total_amount % max_amount < min_amount):
                            nGoal = total_amount // max_amount
                            for i in range(int(nGoal)):
                                entry_amount_list.append(max_amount)
                        else:
                            nGoal = total_amount // max_amount + 1
                            for i in range(int(nGoal - 1)):
                                entry_amount_list.append(max_amount)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            entry_amount_list.append(remain_amount)
                        
                        # 진입 가격은 order_info로 넘겨받음
                        entry_price = order_info.price  
                        isSettingFinish = True
                    
                    # 매매 주문
                    for i in range(int(nGoal - nComplete)):
                        entry_amount = entry_amount_list[nComplete]
                        # order_result = bot.client.create_order(symbol, "limit", side, abs(entry_amount), entry_price)
                        order_result = bot.limit_order(order_info, entry_amount, entry_price)   # 실패 시 재시도는 bot.limit_order 안에서 처리
                        orderID_list.append(order_result["id"])
                        nComplete += 1
                        # 디스코드 로그생성
                        background_tasks.add_task(log, exchange_name, order_result, order_info)
                    
                # 4. 매매가 전부 종료되면 near리스트 업데이트
                near_dic[order_info.base] = orderID_list

            elif order_info.order_name in entrySignal_list:
                # Long or Short 시그널 처리
                # 예시) Long1 시그널 수신
                # 해당 종목이 nearLong1_dic에 존재하는지 확인 -> 존재 시, Long1 리스트에 추가
                
                near_dic = matchNearDic(order_info.order_name)
                entry_list = matchEntryList(order_info.order_name)
                if order_info.base in near_dic:
                    entry_list.append(order_info.base)
                    # [Debug] 트뷰 시그널이 도착했다는 알람 발생
                    if not isSendSignalDiscord:
                        background_tasks.add_task(log_custom_message, order_info, "ENTRY_SIGNAL")
                        isSendSignalDiscord = True
            
            elif order_info.order_name in nextSignal_list:
                # NextCandle 시그널 처리
                # 예시) NextCandle_L1 시그널 수신
                # 해당 종목이 nearLong1_dic에 존재하는지 확인 -> 존재 시, Long1_list에 없으면 미체결주문 체크 -> 미체결주문 취소 & 신규 Long1 주문
                
                # 1. 봉마감 후 재주문이 필요없으면 무시
                near_dic = matchNearDic(order_info.order_name)
                entry_list = matchEntryList(order_info.order_name)
                if order_info.base not in near_dic or order_info.base in entry_list: 
                    return {"result" : "ignore"}

                # 2. 거래소 이름 확인
                exchange_name = order_info.exchange
                if exchange_name != "BINANCE":    # Binance Only
                    return {"result" : "ignore"}

                # 3. 미체결 주문 취소 & 재주문
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol

                orderID_list_old = near_dic[order_info.base]
                for orderID in orderID_list_old:
                    # 미체결 주문 취소
                    order = bot.client.fetch_order(orderID, symbol)
                    if order["status"] == "canceled":
                        amountCanceled = order["amount"]
                        sideCanceled = order["side"]
                    elif order["status"] == "closed":
                        # 트뷰로부터 특정 시그널 손실로 인해 이미 체결된 주문인 경우
                        entry_list.append(order_info.base)
                        background_tasks.add_task(log_custom_message, order_info, "ORDER_CLOSED")
                        return {"result" : "ignore"}
                    else:
                        resultCancel = bot.client.cancel_order(orderID, symbol)
                        if resultCancel["status"] == "canceled":
                            amountCanceled = resultCancel["amount"]
                            sideCanceled = resultCancel["side"]
                            # [Debug] 미체결 주문 취소 후 알람 발생
                            background_tasks.add_task(log_custom_message, order_info, "CANCEL_ORDER")

                    # 재주문
                    order_result = bot.client.create_order(symbol, "limit", sideCanceled, amountCanceled, order_info.price)
                    # order_result = bot.limit_order(order_info, amountCanceled, order_info.price)
                    orderID_list_old.remove(orderID)
                    orderID_list.append(order_result["id"])

                    # 트뷰에서는 청산 시그널로 오기 때문에 디스코드로 알람 보낼때는 진입으로 전환해줌
                    order_info.is_entry = True
                    order_info.is_close = False
                    order_info.is_buy = not order_info.is_buy
                    order_info.is_sell = not order_info.is_sell
                    background_tasks.add_task(log, exchange_name, order_result, order_info)

                # 4. near_dic 오더id 업데이트
                near_dic[order_info.base] = orderID_list

            elif order_info.order_name in closeSignal_list:
                # 청산 시그널 처리
                # 예시) 청산 시그널 수신
                # 해당 종목이 nearLong1_list에 존재하는지 확인 -> 존재 시, 청산 주문 & 미체결 주문 취소 -> 성공 시, 존재하는 모든 리스트에서 제거
                
                # 0. near_ignore_list 초기화
                if order_info.base in nearLong1_ignore_list:
                    nearLong1_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong1 무시 해제")
                if order_info.base in nearLong2_ignore_list:
                    nearLong2_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong2 무시 해제")
                if order_info.base in nearLong3_ignore_list:
                    nearLong3_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong3 무시 해제")
                if order_info.base in nearLong4_ignore_list:
                    nearLong4_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong4 무시 해제")
                if order_info.base in nearShort1_ignore_list:
                    nearShort1_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort1 무시 해제")
                if order_info.base in nearShort2_ignore_list:
                    nearShort2_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort2 무시 해제")
                if order_info.base in nearShort3_ignore_list:
                    nearShort3_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort3 무시 해제")
                if order_info.base in nearShort4_ignore_list:
                    nearShort4_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort4 무시 해제")

                # 1. 안 산 주문에 대한 종료 무시
                if order_info.base not in (list(nearLong1_dic) + list(nearLong2_dic) + list(nearLong3_dic) + list(nearLong4_dic) + \
                                            list(nearShort1_dic) + list(nearShort2_dic) + list(nearShort3_dic) + list(nearShort4_dic)):
                    return {"result" : "ignore"}

                # 2. 거래소 이름 확인
                exchange_name = order_info.exchange
                if exchange_name != "BINANCE":    # Binance Only
                    return {"result" : "ignore"}

                # 3. 미체결 주문 취소
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol
                if not isCancelSuccess:
                    bot.client.cancel_all_orders(symbol)
                    isCancelSuccess = True

                # [Debug] 미체결 주문 취소 후 알람 발생
                if not isSendSignalDiscord and isCancelSuccess:
                    background_tasks.add_task(log_custom_message, order_info, "CANCEL_ORDER")
                    isSendSignalDiscord = True

                # 4. 청산 주문
                if order_info.is_close:
                    #############################
                    ## Close 매매코드
                    #############################
                    if not isSettingFinish:
                        # 초기 세팅
                        # total amount를 max_amount로 쪼개기
                        total_amount = bot.get_amount_hatiko(symbol, nMaxLong, nMaxShort)
                        market = bot.client.market(symbol)
                        max_amount = market["limits"]["amount"]["max"] # 지정가 주문 최대 코인개수
                        min_amount = market["limits"]["amount"]["min"] # 지정가 주문 최소 코인개수
                        # max_amount = bot.future_markets[symbol]["limits"]["amount"]["max"] # 지정가 주문 최대 코인개수
                        # min_amount = bot.future_markets[symbol]["limits"]["amount"]["min"]

                        # Set nGoal
                        close_amount_list = []
                        if (total_amount % max_amount < min_amount):
                            nGoal = total_amount // max_amount
                            for i in range(int(nGoal)):
                                close_amount_list.append(max_amount)
                        else:
                            nGoal = total_amount // max_amount + 1
                            for i in range(int(nGoal - 1)):
                                close_amount_list.append(max_amount)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            close_amount_list.append(remain_amount)

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
                            order_result = bot.limit_close(order_info, close_amount, close_price)
                            nComplete += 1
                            background_tasks.add_task(log, exchange_name, order_result, order_info)

                # 4. 매매가 전부 종료된 후 매매종목 리스트 업데이트
                if order_info.base in nearLong1_dic:
                    nearLong1_dic.pop(order_info.base)
                if order_info.base in nearLong2_dic:
                    nearLong2_dic.pop(order_info.base)
                if order_info.base in nearLong3_dic:
                    nearLong3_dic.pop(order_info.base)
                if order_info.base in nearLong4_dic:
                    nearLong4_dic.pop(order_info.base)
                if order_info.base in nearShort1_dic:
                    nearShort1_dic.pop(order_info.base)
                if order_info.base in nearShort2_dic:
                    nearShort2_dic.pop(order_info.base)
                if order_info.base in nearShort3_dic:
                    nearShort3_dic.pop(order_info.base)
                if order_info.base in nearShort4_dic:
                    nearShort4_dic.pop(order_info.base)

                if order_info.base in Long1_list:
                    Long1_list.remove(order_info.base)
                if order_info.base in Long2_list:
                    Long2_list.remove(order_info.base)
                if order_info.base in Long3_list:
                    Long3_list.remove(order_info.base)
                if order_info.base in Long4_list:
                    Long4_list.remove(order_info.base)
                if order_info.base in Short1_list:
                    Short1_list.remove(order_info.base)
                if order_info.base in Short2_list:
                    Short2_list.remove(order_info.base)
                if order_info.base in Short3_list:
                    Short3_list.remove(order_info.base)
                if order_info.base in Short4_list:
                    Short4_list.remove(order_info.base)

            elif order_info.order_name in ignoreSignal_list:
                return {"result" : "ignore"}
        
            else:
                background_tasks.add_task(log_custom_message, order_info, "ORDER_NAME_INCORRECT")
                return {"result" : "ignore"}
            
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
#endregion Old Hatiko Logic





#region 각 거래소별 HatikoInfo
HI_Binance_Spot = HatikoInfo() 
HI_Binance_Future = HatikoInfo()
HI_OKX_Spot = HatikoInfo()
HI_OKX_Future = HatikoInfo()
HI_Bitget_Spot = HatikoInfo()
HI_Bitget_Future = HatikoInfo()
HI_Bybit_Spot = HatikoInfo()
HI_Bybit_Future = HatikoInfo()
#endregion 각 거래소별 HatikoInfo

#region HatikoInfo 관련 메모리 모니터
@ app.get("/hatikoinfo_binance_spot")
async def hatikoinfo_binance_spot():
    global HI_Binance_Spot
    return HI_Binance_Spot.getHatikoInfo()

@ app.get("/hatikoinfo_binance_future")
async def hatikoinfo_binance_future():
    global HI_Binance_Future
    return HI_Binance_Future.getHatikoInfo()

@ app.get("/hatikoinfo_okx_spot")
async def hatikoinfo_okx_spot():
    return HI_OKX_Spot.getHatikoInfo()

@ app.get("/hatikoinfo_okx_future")
async def hatikoinfo_okx_future():
    return HI_OKX_Future.getHatikoInfo()

@ app.get("/hatikoinfo_bybit_spot")
async def hatikoinfo_bybit_spot():
    return HI_Bybit_Spot.getHatikoInfo()

@ app.get("/hatikoinfo_bybit_future")
async def hatikoinfo_bybit_future():
    return HI_Bybit_Future.getHatikoInfo()

@ app.get("/hatikoinfo_Bitget_spot")
async def hatikoinfo_bitget_spot():
    return HI_Bitget_Spot.getHatikoInfo()

@ app.get("/hatikoinfo_bitget_future")
async def hatikoinfo_bitget_future():
    return HI_Bitget_Future.getHatikoInfo()
#endregion Hatiko Limit 관련 메모리 모니터

#region HatikoInfo 리셋
@app.get("/reset_hatikoinfo")
async def reset_hatikoinfo():
    global HI_Binance_Future, HI_Binance_Spot, HI_Bitget_Future, HI_Bitget_Spot, HI_Bybit_Future, HI_Bybit_Spot, HI_OKX_Future, HI_OKX_Spot
    HI_Binance_Spot.resetHatikoInfo()
    HI_Binance_Future.resetHatikoInfo()
    HI_Bitget_Spot.resetHatikoInfo()
    HI_Bitget_Future.resetHatikoInfo()
    HI_Bybit_Spot.resetHatikoInfo()
    HI_Bybit_Future.resetHatikoInfo()
    HI_OKX_Spot.resetHatikoInfo()
    HI_OKX_Future.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_binance_spot")
async def reset_hatikoinfo_binance_spot():
    global HI_Binance_Spot
    HI_Binance_Spot.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_binance_future")
async def reset_hatikoinfo_binance_future():
    global HI_Binance_Future
    HI_Binance_Future.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_bitget_spot")
async def reset_hatikoinfo_bitget_spot():
    global HI_Bitget_Spot
    HI_Bitget_Spot.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_bitget_future")
async def reset_hatikoinfo_bitget_future():
    global HI_Bitget_Future
    HI_Bitget_Future.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_bybit_spot")
async def reset_hatikoinfo_bybit_spot():
    global HI_Bybit_Spot
    HI_Bybit_Spot.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_bybit_future")
async def reset_hatikoinfo_bybit_future():
    global HI_Bybit_Future
    HI_Bybit_Future.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_okx_spot")
async def reset_hatikoinfo_okx_spot():
    global HI_OKX_Spot
    HI_OKX_Spot.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"

@app.get("/reset_hatikoinfo_okx_future")
async def reset_hatikoinfo_okx_future():
    global HI_OKX_Future
    HI_OKX_Future.resetHatikoInfo()
    return "Reset HatikoInfo Complete!!!"
#endregion HatikoInfo 리셋

#region add_nMax
@ app.get("/add_nMaxLong_binance_spot")
async def add_nMaxLong_binance_spot():
    global HI_Binance_Spot
    old = HI_Binance_Spot.nMaxLong
    new = HI_Binance_Spot.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxLong_binance_future")
async def add_nMaxLong_binance_future():
    global HI_Binance_Future
    old = HI_Binance_Future.nMaxLong
    new = HI_Binance_Future.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxLong_bitget_spot")
async def add_nMaxLong_bitget_spot():
    global HI_Bitget_Spot
    old = HI_Bitget_Spot.nMaxLong
    new = HI_Bitget_Spot.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxLong_bitget_future")
async def add_nMaxLong_bitget_future():
    global HI_Bitget_Future
    old = HI_Bitget_Future.nMaxLong
    new = HI_Bitget_Future.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxLong_bybit_spot")
async def add_nMaxLong_bybit_spot():
    global HI_Bybit_Spot
    old = HI_Bybit_Spot.nMaxLong
    new = HI_Bybit_Spot.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxLong_bybit_future")
async def add_nMaxLong_bybit_future():
    global HI_Bybit_Future
    old = HI_Bybit_Future.nMaxLong
    new = HI_Bybit_Future.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxLong_okx_spot")
async def add_nMaxLong_okx_spot():
    global HI_OKX_Spot
    old = HI_OKX_Spot.nMaxLong
    new = HI_OKX_Spot.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxLong_okx_future")
async def add_nMaxLong_okx_future():
    global HI_OKX_Future
    old = HI_OKX_Future.nMaxLong
    new = HI_OKX_Future.add_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/add_nMaxShort_binance_spot")
async def add_nMaxShort_binance_spot():
    global HI_Binance_Spot
    old = HI_Binance_Spot.nMaxShort
    new = HI_Binance_Spot.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/add_nMaxShort_binance_future")
async def add_nMaxShort_binance_future():
    global HI_Binance_Future
    old = HI_Binance_Future.nMaxShort
    new = HI_Binance_Future.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/add_nMaxShort_bitget_spot")
async def add_nMaxShort_bitget_spot():
    global HI_Bitget_Spot
    old = HI_Bitget_Spot.nMaxShort
    new = HI_Bitget_Spot.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/add_nMaxShort_bitget_future")
async def add_nMaxShort_bitget_future():
    global HI_Bitget_Future
    old = HI_Bitget_Future.nMaxShort
    new = HI_Bitget_Future.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/add_nMaxShort_bybit_spot")
async def add_nMaxShort_bybit_spot():
    global HI_Bybit_Spot
    old = HI_Bybit_Spot.nMaxShort
    new = HI_Bybit_Spot.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/add_nMaxShort_bybit_future")
async def add_nMaxShort_bybit_future():
    global HI_Bybit_Future
    old = HI_Bybit_Future.nMaxShort
    new = HI_Bybit_Future.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/add_nMaxShort_okx_spot")
async def add_nMaxShort_okx_spot():
    global HI_OKX_Spot
    old = HI_OKX_Spot.nMaxShort
    new = HI_OKX_Spot.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/add_nMaxShort_okx_future")
async def add_nMaxShort_okx_future():
    global HI_OKX_Future
    old = HI_OKX_Future.nMaxShort
    new = HI_OKX_Future.add_nMaxShort()
    return f"nMaxShort : {old} -> {new}"
#endregion add_nMax

#region subtract_nMax
@ app.get("/subtract_nMaxLong_binance_spot")
async def subtract_nMaxLong_binance_spot():
    global HI_Binance_Spot
    old = HI_Binance_Spot.nMaxLong
    new = HI_Binance_Spot.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxLong_binance_future")
async def subtract_nMaxLong_binance_future():
    global HI_Binance_Future
    old = HI_Binance_Future.nMaxLong
    new = HI_Binance_Future.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxLong_bitget_spot")
async def subtract_nMaxLong_bitget_spot():
    global HI_Bitget_Spot
    old = HI_Bitget_Spot.nMaxLong
    new = HI_Bitget_Spot.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxLong_bitget_future")
async def subtract_nMaxLong_bitget_future():
    global HI_Bitget_Future
    old = HI_Bitget_Future.nMaxLong
    new = HI_Bitget_Future.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxLong_bybit_spot")
async def subtract_nMaxLong_bybit_spot():
    global HI_Bybit_Spot
    old = HI_Bybit_Spot.nMaxLong
    new = HI_Bybit_Spot.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxLong_bybit_future")
async def subtract_nMaxLong_bybit_future():
    global HI_Bybit_Future
    old = HI_Bybit_Future.nMaxLong
    new = HI_Bybit_Future.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxLong_okx_spot")
async def subtract_nMaxLong_okx_spot():
    global HI_OKX_Spot
    old = HI_OKX_Spot.nMaxLong
    new = HI_OKX_Spot.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxLong_okx_future")
async def subtract_nMaxLong_okx_future():
    global HI_OKX_Future
    old = HI_OKX_Future.nMaxLong
    new = HI_OKX_Future.subtract_nMaxLong()
    return f"nMaxLong : {old} -> {new}"

@ app.get("/subtract_nMaxShort_binance_spot")
async def subtract_nMaxShort_binance_spot():
    global HI_Binance_Spot
    old = HI_Binance_Spot.nMaxShort
    new = HI_Binance_Spot.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/subtract_nMaxShort_binance_future")
async def subtract_nMaxShort_binance_future():
    global HI_Binance_Future
    old = HI_Binance_Future.nMaxShort
    new = HI_Binance_Future.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/subtract_nMaxShort_bitget_spot")
async def subtract_nMaxShort_bitget_spot():
    global HI_Bitget_Spot
    old = HI_Bitget_Spot.nMaxShort
    new = HI_Bitget_Spot.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/subtract_nMaxShort_bitget_future")
async def subtract_nMaxShort_bitget_future():
    global HI_Bitget_Future
    old = HI_Bitget_Future.nMaxShort
    new = HI_Bitget_Future.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/subtract_nMaxShort_bybit_spot")
async def subtract_nMaxShort_bybit_spot():
    global HI_Bybit_Spot
    old = HI_Bybit_Spot.nMaxShort
    new = HI_Bybit_Spot.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/subtract_nMaxShort_bybit_future")
async def subtract_nMaxShort_bybit_future():
    global HI_Bybit_Future
    old = HI_Bybit_Future.nMaxShort
    new = HI_Bybit_Future.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/subtract_nMaxShort_okx_spot")
async def subtract_nMaxShort_okx_spot():
    global HI_OKX_Spot
    old = HI_OKX_Spot.nMaxShort
    new = HI_OKX_Spot.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"

@ app.get("/subtract_nMaxShort_okx_future")
async def subtract_nMaxShort_okx_future():
    global HI_OKX_Future
    old = HI_OKX_Future.nMaxShort
    new = HI_OKX_Future.subtract_nMaxShort()
    return f"nMaxShort : {old} -> {new}"
#endregion subtract_nMax

#region 각 거래소별 웹훅 주소 정의
@ app.post("/hatikolimit_binance_spot")
@ app.post("/")
async def hatikolimit_binance_spot(order_info: MarketOrder, background_tasks: BackgroundTasks):
    global HI_Binance_Spot
    nIgnoreLong = 0
    nIgnoreShort = 0
    hatikolimitBase_test(order_info, background_tasks, HI_Binance_Spot, nIgnoreLong, nIgnoreShort)

@ app.post("/hatikolimit_binance_future")
@ app.post("/")
async def hatikolimit_binance_future(order_info: MarketOrder, background_tasks: BackgroundTasks):
    global HI_Binance_Future
    nIgnoreLong = 0
    nIgnoreShort = 0
    hatikolimitBase_test(order_info, background_tasks, HI_Binance_Future, nIgnoreLong, nIgnoreShort)



#endregion 각 거래소별 웹훅 주소 정의


def hatikolimitBase_test(order_info: MarketOrder, background_tasks: BackgroundTasks, hatikoInfo: HatikoInfo, nIgnoreLong: int, nIgnoreShort: int):
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
    """
    global nMaxTry

    # 초기화 단계
    order_result = None
    nGoal = 0
    nComplete = 0
    isSettingFinish = False     # 매매전 ccxt 세팅 flag 
    orderID_list = []           # 오더id 리스트
    isCancelSuccess = False     # 미체결주문 취소성공 여부
    amountCanceled = 0          # 주문 취소한 코인개수(NextCandle 로직에서 사용)
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
                if (order_info.side == "buy" and len(near_ignore_list) < nIgnoreLong) or \
                    (order_info.side == "sell" and len(near_ignore_list) < nIgnoreShort):
                    if not order_info.base in near_ignore_list:
                        near_ignore_list.append(order_info.base)
                    background_tasks.add_task(log_custom_message, order_info, "IGNORE")
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
                if bot.order_info.is_entry:
                    ###################################
                    # Entry 매매 코드
                    ###################################

                    if not isSettingFinish:   # 초기 세팅
                        symbol = order_info.unified_symbol
                        if order_info.is_futures and order_info.leverage is not None: 
                            bot.set_leverage(order_info.leverage, symbol)

                        # 진입수량 설정
                        entryRate = hatikoInfo.calcEntryRate(hatikoInfo.nMaxLong, safetyMarginPercent=1) if order_info.is_spot else 0 # entryCash / FreeCash  # 현물에서 사용
                        total_amount = bot.get_amount_hatiko(symbol, hatikoInfo.nMaxLong, hatikoInfo.nMaxShort, entryRate)
                        market = bot.client.market(symbol)
                        max_amount = market["limits"]["amount"]["max"] # 지정가 주문 최대 코인개수  # float
                        min_amount = market["limits"]["amount"]["min"] # 지정가 주문 최소 코인개수  # float

                        # Set nGoal
                        entry_amount_list = []
                        if (total_amount % max_amount < min_amount):
                            nGoal = total_amount // max_amount
                            for i in range(int(nGoal)):
                                entry_amount_list.append(max_amount)
                        else:
                            nGoal = total_amount // max_amount + 1
                            for i in range(int(nGoal - 1)):
                                entry_amount_list.append(max_amount)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            entry_amount_list.append(remain_amount)
                        
                        # 진입 가격은 order_info로 넘겨받음
                        entry_price = order_info.price  
                        isSettingFinish = True
                    
                    # 매매 주문
                    for i in range(int(nGoal - nComplete)):
                        entry_amount = entry_amount_list[nComplete]
                        # order_result = bot.client.create_order(symbol, "limit", side, abs(entry_amount), entry_price)
                        order_result = bot.limit_order(order_info, entry_amount, entry_price)   # 실패 시 재시도는 bot.limit_order 안에서 처리
                        orderID_list.append(order_result["id"])
                        nComplete += 1
                        # 디스코드 로그생성
                        background_tasks.add_task(log, exchange_name, order_result, order_info)
                    
                # 4. 매매가 전부 종료되면 near리스트 업데이트
                near_dic[order_info.base] = orderID_list

            elif order_info.order_name in HatikoInfo.entrySignal_list:
                # Long or Short 시그널 처리
                # 예시) Long1 시그널 수신
                # 해당 종목이 nearLong1_dic에 존재하는지 확인 -> 존재 시, Long1 리스트에 추가
                
                near_dic = hatikoInfo.matchNearDic(order_info.order_name)
                entry_list = hatikoInfo.matchEntryList(order_info.order_name)
                if order_info.base in near_dic:
                    entry_list.append(order_info.base)
                    # [Debug] 트뷰 시그널이 도착했다는 알람 발생
                    if not isSendSignalDiscord:
                        background_tasks.add_task(log_custom_message, order_info, "ENTRY_SIGNAL")
                        isSendSignalDiscord = True

            elif order_info.order_name in HatikoInfo.nextSignal_list:
                # NextCandle 시그널 처리
                # 예시) NextCandle_L1 시그널 수신
                # 해당 종목이 nearLong1_dic에 존재하는지 확인 -> 존재 시, Long1_list에 없으면 미체결주문 체크 -> 미체결주문 취소 & 신규 Long1 주문
                
                # 1. 봉마감 후 재주문이 필요없으면 무시
                near_dic = hatikoInfo.matchNearDic(order_info.order_name)
                entry_list = hatikoInfo.matchEntryList(order_info.order_name)
                if order_info.base not in near_dic or order_info.base in entry_list: 
                    return {"result" : "ignore"}

                # 3. 미체결 주문 취소 & 재주문
                exchange_name = order_info.exchange
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol

                orderID_list_old = near_dic[order_info.base]
                for orderID in orderID_list_old:
                    # 미체결 주문 취소
                    order = bot.client.fetch_order(orderID, symbol)
                    if order["status"] == "canceled":
                        amountCanceled = order["amount"]
                        sideCanceled = order["side"]
                    elif order["status"] == "closed":
                        # 트뷰로부터 특정 시그널 손실로 인해 이미 체결된 주문인 경우
                        entry_list.append(order_info.base)
                        background_tasks.add_task(log_custom_message, order_info, "ORDER_CLOSED")
                        return {"result" : "ignore"}
                    else:
                        resultCancel = bot.client.cancel_order(orderID, symbol)
                        if resultCancel["status"] == "canceled":
                            amountCanceled = resultCancel["amount"]
                            sideCanceled = resultCancel["side"]
                            # [Debug] 미체결 주문 취소 후 알람 발생
                            background_tasks.add_task(log_custom_message, order_info, "CANCEL_ORDER")

                    # 재주문
                    order_result = bot.client.create_order(symbol, "limit", sideCanceled, amountCanceled, order_info.price)
                    # order_result = bot.limit_order(order_info, amountCanceled, order_info.price)
                    orderID_list_old.remove(orderID)
                    orderID_list.append(order_result["id"])

                    # 트뷰에서는 청산 시그널로 오기 때문에 디스코드로 알람 보낼때는 진입으로 전환해줌
                    order_info.is_entry = True
                    order_info.is_close = False
                    order_info.is_buy = not order_info.is_buy
                    order_info.is_sell = not order_info.is_sell
                    background_tasks.add_task(log, exchange_name, order_result, order_info)

                # 4. near_dic 오더id 업데이트
                near_dic[order_info.base] = orderID_list

            elif order_info.order_name in HatikoInfo.closeSignal_list:
                # 청산 시그널 처리
                # 예시) 청산 시그널 수신
                # 해당 종목이 nearLong1_list에 존재하는지 확인 -> 존재 시, 청산 주문 & 미체결 주문 취소 -> 성공 시, 존재하는 모든 리스트에서 제거
                
                # 0. near_ignore_list 초기화
                if order_info.base in hatikoInfo.nearLong1_ignore_list:
                    hatikoInfo.nearLong1_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong1 무시 해제")
                if order_info.base in hatikoInfo.nearLong2_ignore_list:
                    hatikoInfo.nearLong2_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong2 무시 해제")
                if order_info.base in hatikoInfo.nearLong3_ignore_list:
                    hatikoInfo.nearLong3_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong3 무시 해제")
                if order_info.base in hatikoInfo.nearLong4_ignore_list:
                    hatikoInfo.nearLong4_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearLong4 무시 해제")
                if order_info.base in hatikoInfo.nearShort1_ignore_list:
                    hatikoInfo.nearShort1_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort1 무시 해제")
                if order_info.base in hatikoInfo.nearShort2_ignore_list:
                    hatikoInfo.nearShort2_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort2 무시 해제")
                if order_info.base in hatikoInfo.nearShort3_ignore_list:
                    hatikoInfo.nearShort3_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort3 무시 해제")
                if order_info.base in hatikoInfo.nearShort4_ignore_list:
                    hatikoInfo.nearShort4_ignore_list.remove(order_info.base)
                    log_message(f"{order_info.base} nearShort4 무시 해제")

                # 1. 안 산 주문에 대한 종료 무시
                if order_info.base not in (list(hatikoInfo.nearLong1_dic) + list(hatikoInfo.nearLong2_dic) + list(hatikoInfo.nearLong3_dic) + list(hatikoInfo.nearLong4_dic) + \
                                            list(hatikoInfo.nearShort1_dic) + list(hatikoInfo.nearShort2_dic) + list(hatikoInfo.nearShort3_dic) + list(hatikoInfo.nearShort4_dic)):
                    return {"result" : "ignore"}

                # 2. 거래소 이름 확인
                exchange_name = order_info.exchange
                if exchange_name != "BINANCE":    # Binance Only
                    return {"result" : "ignore"}

                # 3. 미체결 주문 취소
                bot = get_bot(exchange_name, order_info.kis_number)
                bot.init_info(order_info)
                symbol = order_info.unified_symbol
                if not isCancelSuccess:
                    bot.client.cancel_all_orders(symbol)
                    isCancelSuccess = True

                # [Debug] 미체결 주문 취소 후 알람 발생
                if not isSendSignalDiscord and isCancelSuccess:
                    background_tasks.add_task(log_custom_message, order_info, "CANCEL_ORDER")
                    isSendSignalDiscord = True

                # 4. 청산 주문
                if order_info.is_close:
                    #############################
                    ## Close 매매코드
                    #############################
                    if not isSettingFinish:
                        # 초기 세팅
                        # total amount를 max_amount로 쪼개기
                        total_amount = bot.get_amount_hatiko(symbol, hatikoInfo.nMaxLong, hatikoInfo.nMaxShort)
                        market = bot.client.market(symbol)
                        max_amount = market["limits"]["amount"]["max"] # 지정가 주문 최대 코인개수
                        min_amount = market["limits"]["amount"]["min"] # 지정가 주문 최소 코인개수

                        # Set nGoal
                        close_amount_list = []
                        if (total_amount % max_amount < min_amount):
                            nGoal = total_amount // max_amount
                            for i in range(int(nGoal)):
                                close_amount_list.append(max_amount)
                        else:
                            nGoal = total_amount // max_amount + 1
                            for i in range(int(nGoal - 1)):
                                close_amount_list.append(max_amount)
                            remain_amount = float(bot.client.amount_to_precision(symbol, total_amount % max_amount))
                            close_amount_list.append(remain_amount)

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
                            order_result = bot.limit_close(order_info, close_amount, close_price)
                            nComplete += 1
                            background_tasks.add_task(log, exchange_name, order_result, order_info)

                # 4. 매매가 전부 종료된 후 매매종목 리스트 업데이트
                if order_info.base in hatikoInfo.nearLong1_dic:
                    hatikoInfo.nearLong1_dic.pop(order_info.base)
                if order_info.base in hatikoInfo.nearLong2_dic:
                    hatikoInfo.nearLong2_dic.pop(order_info.base)
                if order_info.base in hatikoInfo.nearLong3_dic:
                    hatikoInfo.nearLong3_dic.pop(order_info.base)
                if order_info.base in hatikoInfo.nearLong4_dic:
                    hatikoInfo.nearLong4_dic.pop(order_info.base)
                if order_info.base in hatikoInfo.nearShort1_dic:
                    hatikoInfo.nearShort1_dic.pop(order_info.base)
                if order_info.base in hatikoInfo.nearShort2_dic:
                    hatikoInfo.nearShort2_dic.pop(order_info.base)
                if order_info.base in hatikoInfo.nearShort3_dic:
                    hatikoInfo.nearShort3_dic.pop(order_info.base)
                if order_info.base in hatikoInfo.nearShort4_dic:
                    hatikoInfo.nearShort4_dic.pop(order_info.base)

                if order_info.base in hatikoInfo.Long1_list:
                    hatikoInfo.Long1_list.remove(order_info.base)
                if order_info.base in hatikoInfo.Long2_list:
                    hatikoInfo.Long2_list.remove(order_info.base)
                if order_info.base in hatikoInfo.Long3_list:
                    hatikoInfo.Long3_list.remove(order_info.base)
                if order_info.base in hatikoInfo.Long4_list:
                    hatikoInfo.Long4_list.remove(order_info.base)
                if order_info.base in hatikoInfo.Short1_list:
                    hatikoInfo.Short1_list.remove(order_info.base)
                if order_info.base in hatikoInfo.Short2_list:
                    hatikoInfo.Short2_list.remove(order_info.base)
                if order_info.base in hatikoInfo.Short3_list:
                    hatikoInfo.Short3_list.remove(order_info.base)
                if order_info.base in hatikoInfo.Short4_list:
                    hatikoInfo.Short4_list.remove(order_info.base)

            elif order_info.order_name in HatikoInfo.ignoreSignal_list:
                return {"result" : "ignore"}

            else:
                background_tasks.add_task(log_custom_message, order_info, "ORDER_NAME_INCORRECT")
                return {"result" : "ignore"}

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

#endregion ############################### Hatiko ###############################


#region ############################### 켈트너 + Hatiko in Upbit #################################

kctrend_long_list = []   # 켈트너 추세 전략 Long 진입 중인 종목들 list

hatiko_long1_list = []    # Hatiko 전략 Long1 진입 중인 종목들 list
hatiko_long2_list = []    # Hatiko 전략 Long2 진입 중인 종목들 list
hatiko_long3_list = []    # Hatiko 전략 Long3 진입 중인 종목들 list
hatiko_long4_list = []    # Hatiko 전략 Long4 진입 중인 종목들 list

@ app.get("/initkctrendandhatiko")
async def initkctrendandhatiko():
    global kctrend_long_list, hatiko_long1_list, hatiko_long2_list, hatiko_long3_list, hatiko_long4_list

    # kctrend + hatiko 관련 전역변수 초기화
    kctrend_long_list = []

    hatiko_long1_list = []
    hatiko_long2_list = []
    hatiko_long3_list = []
    hatiko_long4_list = []
    
    return "Intialize kctrend & hatiko Variables Completed!!!"

@ app.get("/kctrendandhatikoinfo")
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
            if not order_info.base in kctrend_long_list:
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
            if not order_info.base in kctrend_long_list:
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

@ app.post("/kctrendandhatikolimit")
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
            if not order_info.base in kctrend_long_list:
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
