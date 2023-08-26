from fastapi.exception_handlers import (
    request_validation_exception_handler,
)
from pprint import pprint
from fastapi import FastAPI, Request, status, BackgroundTasks
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
import httpx
from exchange.stock.kis import KoreaInvestment
from exchange.model import MarketOrder, PriceRequest, HedgeData, OrderRequest
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
	log_custom_message
)
import traceback
from exchange import get_exchange, log_message, db, settings, get_bot, pocket
import ipaddress
import os
import sys
from devtools import debug

VERSION = "POA : 0.1.1, Hatiko : 230826"
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
    res = f"exchange(Literal str)  : {order_info.exchange}\n" \
        f"base(str) : {order_info.base}\n" \
        f"quote(Literal str) : {order_info.quote}\n" \
        f"type(Literal str) : {order_info.type}\n" \
        f"type(Literal str) : {order_info.type}\n" \
        f"side(Literal str) : {order_info.side}\n" \
        f"amount(float) : {order_info.amount}\n" \
        f"price(float) : {order_info.price}\n" \
        f"cost(float) : {order_info.cost}\n" \
        f"percent(float) : {order_info.percent}\n" \
        f"amount_by_percent(float) : {order_info.amount_by_percent}\n" \
        f"leverage(int) : {order_info.leverage}\n" \
        f"stop_price(float) : {order_info.stop_price}\n" \
        f"profit_price(float) : {order_info.profit_price}\n" \
        f"order_name(str) : {order_info.order_name}\n" \
        f"kis_number(int) : {order_info.kis_number}\n" \
        f"hedge(str) : {order_info.hedge}\n" \
        f"unified_symbol(str) : {order_info.unified_symbol}\n" \
        f"is_crypto(bool) : {order_info.is_crypto}\n" \
        f"is_stock(bool) : {order_info.is_stock}\n" \
        f"is_spot(bool) : {order_info.is_spot}\n" \
        f"is_futures(bool) : {order_info.is_futures}\n" \
        f"is_coinm(bool) : {order_info.is_coinm}\n" \
        f"is_entry(bool) : {order_info.is_entry}\n" \
        f"is_close(bool) : {order_info.is_close}\n" \
        f"is_buy(bool) : {order_info.is_buy}\n" \
        f"is_sell(bool) : {order_info.is_sell}\n" \
        f"is_contract(bool) : {order_info.is_contract}\n" \
        f"contract_size(float) : {order_info.contract_size}\n" \
        f"margin_mode(str) : {order_info.margin_mode}\n"

    # res = {
    #     "exchange(Literal str)"  : str(order_info.exchange),
    #     "base(str)" : str(order_info.base),
    #     "quote(Literal str)" : str(order_info.quote),
    #     "type(Literal str)" : str(order_info.type),
    #     "side(Literal str)" : str(order_info.side),
    #     "amount(float)" : str(order_info.amount),
    #     "price(float)" : str(order_info.price),
    #     "cost(float)" : str(order_info.cost),
    #     "percent(float)" : str(order_info.percent),
    #     "amount_by_percent(float)" : str(order_info.amount_by_percent),
    #     "leverage(int)" : str(order_info.leverage),
    #     "stop_price(float)" : str(order_info.stop_price),
    #     "profit_price(float)" : str(order_info.profit_price),
    #     "order_name(str)" : str(order_info.order_name),
    #     "kis_number(int)" : str(order_info.kis_number),
    #     "hedge(str)" : str(order_info.hedge),
    #     "unified_symbol(str)" : str(order_info.unified_symbol),
    #     "is_crypto(bool)" : str(order_info.is_crypto),
    #     "is_stock(bool)" : str(order_info.is_stock),
    #     "is_spot(bool)" : str(order_info.is_spot),
    #     "is_futures(bool)" : str(order_info.is_futures),
    #     "is_coinm(bool)" : str(order_info.is_coinm),
    #     "is_entry(bool)" : str(order_info.is_entry),
    #     "is_close(bool)" : str(order_info.is_close),
    #     "is_buy(bool)" : str(order_info.is_buy),
    #     "is_sell(bool)" : str(order_info.is_sell),
    #     "is_contract(bool)" : str(order_info.is_contract),
    #     "contract_size(float)" : str(order_info.contract_size),
    #     "margin_mode(str)" : str(order_info.margin_mode)
    #     }
    return res

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
    
# Hatiko Limit 관련 메모리 모니터
@ app.get("/hatikolimitInfo")
async def hatikolimitInfo():
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
        "Short4_list" : str(Short4_list)
        }
    return res

# Hatiko Limit 관련 전역변수 초기화
@ app.get("/inithatiko")
async def inithatiko():
    global nearLong1_dic, nearLong2_dic, nearLong3_dic, nearLong4_dic
    global nearShort1_dic, nearShort2_dic, nearShort3_dic, nearShort4_dic
    global Long1_list, Long2_list, Long3_list, Long4_list
    global Short1_list, Short2_list, Short3_list, Short4_list

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

    res = "Intialize Hatiko Variables Completed!!!"
    return res

@ app.post("/hatikolimit")
@ app.post("/")
async def hatikolimit(order_info: MarketOrder, background_tasks: BackgroundTasks):
    nMaxLong = 1
    nMaxShort = 1
    hatikolimitBase(order_info, background_tasks, nMaxLong, nMaxShort)

@ app.post("/hatikolimit2")
@ app.post("/")
async def hatikolimit2(order_info: MarketOrder, background_tasks: BackgroundTasks):
    nMaxLong = 2
    nMaxShort = 1
    hatikolimitBase(order_info, background_tasks, nMaxLong, nMaxShort)

@ app.post("/hatikolimit4")
@ app.post("/")
async def hatikolimit4(order_info: MarketOrder, background_tasks: BackgroundTasks):
    nMaxLong = 4
    nMaxShort = 1
    hatikolimitBase(order_info, background_tasks, nMaxLong, nMaxShort)

def hatikolimitBase(order_info: MarketOrder, background_tasks: BackgroundTasks, nMaxLong: int, nMaxShort: int):
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

            if order_info.order_name in entrySignal_list:
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
            
            if order_info.order_name in nextSignal_list:
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

            if order_info.order_name in closeSignal_list:
                # 청산 시그널 처리
                # 예시) 청산 시그널 수신
                # 해당 종목이 nearLong1_list에 존재하는지 확인 -> 존재 시, 청산 주문 & 미체결 주문 취소 -> 성공 시, 존재하는 모든 리스트에서 제거
                
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

            if order_info.order_name in ignoreSignal_list:
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






