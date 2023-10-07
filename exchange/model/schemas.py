from pydantic import BaseModel, BaseSettings, validator, root_validator
from typing import Literal
import os
from pathlib import Path
from enum import Enum
from devtools import debug

CRYPTO_LITERAL = Literal["BINANCE", "UPBIT", "BYBIT", "BITGET", "OKX"]


STOCK_LITERAL = Literal[
    "KRX",
    "NASDAQ",
    "NYSE",
    "AMEX",
]


EXCHANGE_LITERAL = Literal[
    "BINANCE",
    "UPBIT",
    "BYBIT",
    "BITGET",
    "OKX",
    "KRX",
    "NASDAQ",
    "NYSE",
    "AMEX",
]

QUOTE_LITERAL = Literal["USDT", "USDT.P", "USDTPERP", "BUSD", "BUSD.P", "BUSDPERP", "KRW", "USD", "USD.P"]

SIDE_LITERAL = Literal["buy", "sell", "entry/buy", "entry/sell", "close/buy", "close/sell"]


def find_env_file():
    current_path = os.path.abspath(__file__)
    while True:
        parent_path = os.path.dirname(current_path)
        env_path = os.path.join(parent_path, ".env")
        dev_env_path = os.path.join(parent_path, ".env.dev")
        if os.path.isfile(dev_env_path):
            return dev_env_path
        elif os.path.isfile(env_path):
            return env_path
        if parent_path == current_path:
            break
        current_path = parent_path
    return None


env_path = find_env_file()


CRYPTO_EXCHANGES = ("BINANCE", "UPBIT", "BYBIT", "BITGET", "OKX")

STOCK_EXCHANGES = (
    "KRX",
    "NASDAQ",
    "NYSE",
    "AMEX",
)

COST_BASED_ORDER_EXCHANGES = ("UPBIT", "BYBIT", "BITGET")

NO_ORDER_AMOUNT_OUTPUT_EXCHANGES = (
    "BITGET",
    "KRX",
    "NASDAQ",
    "NYSE",
    "AMEX",
)

# "BITGET", "KRX", "NASDAQ", "AMEX", "NYSE")


crypto_futures_code = ("PERP", ".P")

# Literal[
#     "KRW", "USDT", "USDTPERP", "BUSD", "BUSDPERP", "USDT.P", "USD", "BUSD.P"
# ]


class Settings(BaseSettings):
    PASSWORD: str
    WHITELIST: list[str] | None = None
    PORT: int | None = None
    DISCORD_WEBHOOK_URL: str | None = None
    UPBIT_KEY: str | None = None
    UPBIT_SECRET: str | None = None
    BINANCE_KEY: str | None = None
    BINANCE_SECRET: str | None = None
    BYBIT_KEY: str | None = None
    BYBIT_SECRET: str | None = None
    BITGET_KEY: str | None = None
    BITGET_SECRET: str | None = None
    BITGET_PASSPHRASE: str | None = None
    OKX_KEY: str | None = None
    OKX_SECRET: str | None = None
    OKX_PASSPHRASE: str | None = None
    KIS1_ACCOUNT_NUMBER: str | None = None
    KIS1_ACCOUNT_CODE: str | None = None
    KIS1_KEY: str | None = None
    KIS1_SECRET: str | None = None
    KIS2_ACCOUNT_NUMBER: str | None = None
    KIS2_ACCOUNT_CODE: str | None = None
    KIS2_KEY: str | None = None
    KIS2_SECRET: str | None = None
    KIS3_ACCOUNT_NUMBER: str | None = None
    KIS3_ACCOUNT_CODE: str | None = None
    KIS3_KEY: str | None = None
    KIS3_SECRET: str | None = None
    KIS4_ACCOUNT_NUMBER: str | None = None
    KIS4_ACCOUNT_CODE: str | None = None
    KIS4_KEY: str | None = None
    KIS4_SECRET: str | None = None
    DB_ID: str = "poa@admin.com"
    DB_PASSWORD: str = "poabot!@#$"

    class Config:
        env_file = env_path  # ".env"
        env_file_encoding = "utf-8"


def get_extra_order_info(order_info):
    extra_order_info = {
        "is_futures": None,
        "is_crypto": None,
        "is_stock": None,
        "is_spot": None,
        "is_entry": None,
        "is_close": None,
        "is_buy": None,
        "is_sell": None,
    }
    if order_info["exchange"] in CRYPTO_EXCHANGES:
        extra_order_info["is_crypto"] = True
        if any([order_info["quote"].endswith(code) for code in crypto_futures_code]):
            extra_order_info["is_futures"] = True
        else:
            extra_order_info["is_spot"] = True

    elif order_info["exchange"] in STOCK_EXCHANGES:
        extra_order_info["is_stock"] = True

    if order_info["side"] in ("entry/buy", "entry/sell"):
        extra_order_info["is_entry"] = True
        _side = order_info["side"].split("/")[-1]
        if _side == "buy":
            extra_order_info["is_buy"] = True
        elif _side == "sell":
            extra_order_info["is_sell"] = True
    elif order_info["side"] in ("close/buy", "close/sell"):
        extra_order_info["is_close"] = True
        _side = order_info["side"].split("/")[-1]
        if _side == "buy":
            extra_order_info["is_buy"] = True
        elif _side == "sell":
            extra_order_info["is_sell"] = True
    elif order_info["side"] == "buy":
        extra_order_info["is_buy"] = True
    elif order_info["side"] == "sell":
        extra_order_info["is_sell"] = True

    return extra_order_info


def parse_side(side: str):
    if side.startswith("entry/") or side.startswith("close/"):
        return side.split("/")[-1]
    else:
        return side


def parse_quote(quote: str):
    if quote.endswith(".P"):
        return quote.replace(".P", "")
    else:
        return quote


class OrderRequest(BaseModel):
    exchange: EXCHANGE_LITERAL
    base: str
    quote: QUOTE_LITERAL
    # QUOTE
    type: Literal["market", "limit"] = "market"
    side: SIDE_LITERAL
    amount: float | None = None
    price: float | None = None
    cost: float | None = None
    percent: float | None = None
    amount_by_percent: float | None = None
    leverage: int | None = None
    stop_price: float | None = None
    profit_price: float | None = None
    order_name: str = "주문"
    kis_number: int | None = 1
    hedge: str | None = None
    unified_symbol: str | None = None
    is_crypto: bool | None = None
    is_stock: bool | None = None
    is_spot: bool | None = None
    is_futures: bool | None = None
    is_coinm: bool | None = None
    is_entry: bool | None = None
    is_close: bool | None = None
    is_buy: bool | None = None
    is_sell: bool | None = None
    is_contract: bool | None = None
    contract_size: float | None = None
    margin_mode: str | None = None

    class Config:
        use_enum_values = True

    @root_validator(pre=True)
    def root_validate(cls, values):
        # "NaN" to None
        for key, value in values.items():
            if value in ("NaN", ""):
                values[key] = None

        values |= get_extra_order_info(values)

        values["side"] = parse_side(values["side"])
        values["quote"] = parse_quote(values["quote"])
        base = values["base"]
        quote = values["quote"]
        unified_symbol = f"{base}/{quote}"
        exchange = values["exchange"]
        if values["is_futures"]:
            if quote == "USD":
                unified_symbol = f"{base}/{quote}:{base}"
                values["is_coinm"] = True
            else:
                unified_symbol = f"{base}/{quote}:{quote}"

        if not values["is_stock"]:
            values["unified_symbol"] = unified_symbol

        if values["exchange"] in STOCK_EXCHANGES:
            values["is_stock"] = True
        # debug("after", values)
        return values


class OrderBase(OrderRequest):
    password: str

    @validator("password")
    def password_validate(cls, v):
        setting = Settings()
        if v != setting.PASSWORD:
            raise ValueError("비밀번호가 틀렸습니다")
        return v


class MarketOrder(OrderBase):
    price: float | None = None
    type: Literal["market"] = "market"


class PriceRequest(BaseModel):
    exchange: EXCHANGE_LITERAL
    base: str
    quote: QUOTE_LITERAL
    is_crypto: bool | None = None
    is_stock: bool | None = None
    is_futures: bool | None = None

    @root_validator(pre=True)
    def root_validate(cls, values):
        # "NaN" to None
        for key, value in values.items():
            if value in ("NaN", ""):
                values[key] = None

        values |= get_extra_order_info(values)

        return values


# class PositionRequest(BaseModel):
#     exchange: EXCHANGE_LITERAL
#     base: str
#     quote: QUOTE_LITERAL


class Position(BaseModel):
    exchange: EXCHANGE_LITERAL
    base: str
    quote: QUOTE_LITERAL
    side: Literal["long", "short"]
    amount: float
    entry_price: float
    roe: float


class HedgeData(BaseModel):
    password: str
    exchange: Literal["BINANCE"]
    base: str
    quote: QUOTE_LITERAL = "USDT.P"
    amount: float | None = None
    leverage: int | None = None
    hedge: str

    @validator("password")
    def password_validate(cls, v):
        setting = Settings()
        if v != setting.PASSWORD:
            raise ValueError("비밀번호가 틀렸습니다")
        return v

    @root_validator(pre=True)
    def root_validate(cls, values):
        for key, value in values.items():
            if key in ("exchange", "base", "quote", "hedge"):
                values[key] = value.upper()
        return values

##############################################################################
# by PTW
##############################################################################

class ArbiData(BaseModel):
    password: str
    exchange_long: Literal["BINANCE", "BYBIT", "BITGET", "OKX"]
    exchange_short: Literal["BINANCE", "BYBIT", "BITGET", "OKX"]
    base: str
    quote: QUOTE_LITERAL = "USDT.P"
    amount: float | None = None
    leverage: int | None = None
    hedge: str

    @validator("password")
    def password_validate(cls, v):
        setting = Settings()
        if v != setting.PASSWORD:
            raise ValueError("비밀번호가 틀렸습니다")
        return v

    @root_validator(pre=True)
    def root_validate(cls, values):
        for key, value in values.items():
            if key in ("exchange_long", "exchange_short" "base", "quote", "hedge"):
                values[key] = value.upper()
        return values

class HatikoOrder(MarketOrder):
    # order mode
    mode: Literal["Near", "NextCandle", "Close"] | None = None

    # Long Envelope Price
    price_L1: float | None = None
    price_L2: float | None = None
    price_L3: float | None = None
    price_L4: float | None = None

    # Short Envelope Price
    price_S1: float | None = None
    price_S2: float | None = None
    price_S3: float | None = None
    price_S4: float | None = None

    # Close Price
    price_LC: float | None = None
    price_SC: float | None = None

    # Order Name Mapping
    order_name_map = {
        "price_L1": {"Near": "nearLong1",     "NextCandle": "NextCandle_L1"},
        "price_L2": {"Near": "nearLong2",     "NextCandle": "NextCandle_L2"},
        "price_L3": {"Near": "nearLong3",     "NextCandle": "NextCandle_L3"},
        "price_L4": {"Near": "nearLong4",     "NextCandle": "NextCandle_L4"},
        "price_S1": {"Near": "nearShort1",    "NextCandle": "NextCandle_S1"},
        "price_S2": {"Near": "nearShort2",    "NextCandle": "NextCandle_S2"},
        "price_S3": {"Near": "nearShort3",    "NextCandle": "NextCandle_S3"},
        "price_S4": {"Near": "nearShort4",    "NextCandle": "NextCandle_S4"},
        "price_LC": {"Close": "close_Longs",  "NextCandle": "NextCandle_LF"},
        "price_SC": {"Close": "close_Shorts", "NextCandle": "NextCandle_SF"},
        }

    @root_validator(pre=True)
    def root_validate(cls, values):
        super().root_validate(values)
        if values["is_futures"]:
            if values["mode"] == "Near":
                values["is_entry"] = True # entry_order
            elif values["mode"] in ("NextCandle", "Close"):
                values["is_close"] = True # close_order
        values["margin_mode"] = "cross" # cross mode - for okx future close order
        return values

class IndividualOrder:
    """
    MarketOrder와 동일한 파라미터 이름을 가진 "인스턴스" 멤버변수를 가지고 있다.
    """
    def __init__(self, order_info: MarketOrder):
        # OrderRequest 정적 변수
        self.exchange = order_info.exchange
        self.base = order_info.base
        self.quote = order_info.quote
        self.type = order_info.type
        self.side = order_info.side
        self.amount = order_info.amount
        self.price = order_info.price
        self.cost = order_info.cost
        self.percent = order_info.percent
        self.amount_by_percent = order_info.amount_by_percent
        self.leverage = order_info.leverage
        self.stop_price = order_info.stop_price
        self.profit_price = order_info.profit_price
        self.order_name = order_info.order_name
        self.kis_number = order_info.kis_number
        self.hedge = order_info.hedge
        self.unified_symbol = order_info.unified_symbol
        self.is_crypto = order_info.is_crypto
        self.is_stock = order_info.is_stock
        self.is_spot = order_info.is_spot
        self.is_futures = order_info.is_futures
        self.is_coinm = order_info.is_coinm
        self.is_entry = order_info.is_entry
        self.is_close = order_info.is_close
        self.is_buy = order_info.is_buy
        self.is_sell = order_info.is_sell
        self.is_contract = order_info.is_contract
        self.contract_size = order_info.contract_size
        self.margin_mode = order_info.margin_mode

        # OrderBase 정적 변수
        self.password = order_info.password

class HatikoInfo:
    # [static] order_name 리스트
    nearSignal_list = ["nearLong1", "nearLong2", "nearLong3", "nearLong4",
                       "nearShort1", "nearShort2", "nearShort3", "nearShort4"]
    entrySignal_list = ["Long1", "Long2", "Long3", "Long4",
                        "Short1", "Short2", "Short3", "Short4"]
    nextSignal_list = ["NextCandle_L1", "NextCandle_L2", "NextCandle_L3", "NextCandle_L4",
                       "NextCandle_S1", "NextCandle_S2", "NextCandle_S3", "NextCandle_S4"]
    nextCloseSignal_list = ["NextCandle_LF", "NextCandle_SF"]
    closeSignal_list = ["close Longs on open", "close Shorts on open",
                        "TakeProfit_nearL1", "TakeProfit_nearL2", "TakeProfit_nearL3", "TakeProfit_nearL4", 
                        "TakeProfit_nearS1", "TakeProfit_nearS2", "TakeProfit_nearS3", "TakeProfit_nearS4", 
                        "TakeProfit_L1", "TakeProfit_L2", "TakeProfit_L3", "TakeProfit_L4",
                        "TakeProfit_S1", "TakeProfit_S2", "TakeProfit_S3", "TakeProfit_S4",
                        "close_Longs", "close_Shorts"]
    ignoreSignal_list = ["Long_Flag", "Short_Flag", "Long_Flag_Cancel", "Short_Flag_Cancel"]
    
    def __init__(self, nMaxLong=2, nMaxShort=1, nIgnoreLong=0, nIgnoreShort=0):
        # 종목 개수 관리
        self.nMaxLong = nMaxLong
        self.nMaxShort = nMaxShort
        self.nIgnoreLong = nIgnoreLong
        self.nIgnoreShort = nIgnoreShort

        # 청산당할 MDD
        self.liquidationMDD = 80.0  # 80% MDD에 도달하면 청산예정

        # 지정가 Hatiko용 near시그널 딕셔너리
        # base(종목명) : orderID_list(오더id 리스트)
        self.nearLong1_dic = {}
        self.nearLong2_dic = {}
        self.nearLong3_dic = {}
        self.nearLong4_dic = {}
        self.nearShort1_dic = {}
        self.nearShort2_dic = {}
        self.nearShort3_dic = {}
        self.nearShort4_dic = {}

        # 지정가 Hatiko용 entry시그널 리스트
        self.Long1_list = []
        self.Long2_list = []
        self.Long3_list = []
        self.Long4_list = []
        self.Short1_list = []
        self.Short2_list = []
        self.Short3_list = []
        self.Short4_list = []

        # 지정가 Hatiko용 무시할 시그널 리스트
        self.nearLong1_ignore_list = []
        self.nearLong2_ignore_list = []
        self.nearLong3_ignore_list = []
        self.nearLong4_ignore_list = []
        self.nearShort1_ignore_list = []
        self.nearShort2_ignore_list = []
        self.nearShort3_ignore_list = []
        self.nearShort4_ignore_list = []        

        # 지정가 Hatiko용 closePrice 딕셔너리
        # base(종목명) : closePrice(청산가격)
        self.closePrice_dic = {} # 미리청산 기능 사용 시 필요
    
    #region match 함수

    def matchNearDic(self, order_name):
        """
        order_name에 따라 해당하는 near딕셔너리를 반환
        예시) input : "NextCandle_L1" -> output : "nearLong1_dic"
        """
        if order_name in ["nearLong1", "Long1", "NextCandle_L1"]:
            return self.nearLong1_dic
        if order_name in ["nearLong2", "Long2", "NextCandle_L2"]:
            return self.nearLong2_dic
        if order_name in ["nearLong3", "Long3", "NextCandle_L3"]:
            return self.nearLong3_dic
        if order_name in ["nearLong4", "Long4", "NextCandle_L4"]:
            return self.nearLong4_dic
        if order_name in ["nearShort1", "Short1", "NextCandle_S1"]:
            return self.nearShort1_dic
        if order_name in ["nearShort2", "Short2", "NextCandle_S2"]:
            return self.nearShort2_dic
        if order_name in ["nearShort3", "Short3", "NextCandle_S3"]:
            return self.nearShort3_dic
        if order_name in ["nearShort4", "Short4", "NextCandle_S4"]:
            return self.nearShort4_dic
        
    def matchEntryList(self, order_name):
        """
        order_name에 따라 해당하는 entry리스트를 반환
        예시) input : "NextCandle_L1" -> output : "Long1"
        """
        if order_name in ["nearLong1", "Long1", "NextCandle_L1"]:
            return self.Long1_list
        if order_name in ["nearLong2", "Long2", "NextCandle_L2"]:
            return self.Long2_list
        if order_name in ["nearLong3", "Long3", "NextCandle_L3"]:
            return self.Long3_list
        if order_name in ["nearLong4", "Long4", "NextCandle_L4"]:
            return self.Long4_list
        if order_name in ["nearShort1", "Short1", "NextCandle_S1"]:
            return self.Short1_list
        if order_name in ["nearShort2", "Short2", "NextCandle_S2"]:
            return self.Short2_list
        if order_name in ["nearShort3", "Short3", "NextCandle_S3"]:
            return self.Short3_list
        if order_name in ["nearShort4", "Short4", "NextCandle_S4"]:
            return self.Short4_list
        
    def matchNearIgnoreList(self, order_name):
        """
        order_name에 따라 해당하는 near_ignore리스트를 반환
        예시) input : "NextCandle_L1" -> output : "nearLong1_ignore_list"
        """
        if order_name in ["nearLong1", "Long1", "NextCandle_L1"]:
            return self.nearLong1_ignore_list
        if order_name in ["nearLong2", "Long2", "NextCandle_L2"]:
            return self.nearLong2_ignore_list
        if order_name in ["nearLong3", "Long3", "NextCandle_L3"]:
            return self.nearLong3_ignore_list
        if order_name in ["nearLong4", "Long4", "NextCandle_L4"]:
            return self.nearLong4_ignore_list
        if order_name in ["nearShort1", "Short1", "NextCandle_S1"]:
            return self.nearShort1_ignore_list
        if order_name in ["nearShort2", "Short2", "NextCandle_S2"]:
            return self.nearShort2_ignore_list
        if order_name in ["nearShort3", "Short3", "NextCandle_S3"]:
            return self.nearShort3_ignore_list
        if order_name in ["nearShort4", "Short4", "NextCandle_S4"]:
            return self.nearShort4_ignore_list
    
    #endregion match 함수

    #region request 호출용 함수

    def getHatikoInfo(self):
            res = {
                "nMaxLong" : str(self.nMaxLong),
                "nMaxShort" : str(self.nMaxShort),
                "nIgnoreLong" : str(self.nIgnoreLong),
                "nIgnoreShort" : str(self.nIgnoreShort),
                "liquidationMDD" : str(self.liquidationMDD),
                "nearLong1_dic"  : str(list(self.nearLong1_dic.keys())),
                "nearLong2_dic"  : str(list(self.nearLong2_dic.keys())),
                "nearLong3_dic"  : str(list(self.nearLong3_dic.keys())),
                "nearLong4_dic"  : str(list(self.nearLong4_dic.keys())),
                "nearShort1_dic" : str(list(self.nearShort1_dic.keys())),
                "nearShort2_dic" : str(list(self.nearShort2_dic.keys())),
                "nearShort3_dic" : str(list(self.nearShort3_dic.keys())),
                "nearShort4_dic" : str(list(self.nearShort4_dic.keys())),
                "Long1_list"  : str(self.Long1_list),
                "Long2_list"  : str(self.Long2_list),
                "Long3_list"  : str(self.Long3_list),
                "Long4_list"  : str(self.Long4_list),
                "Short1_list" : str(self.Short1_list),
                "Short2_list" : str(self.Short2_list),
                "Short3_list" : str(self.Short3_list),
                "Short4_list" : str(self.Short4_list),
                "nearLong1_ignore_list"  : str(self.nearLong1_ignore_list),
                "nearLong2_ignore_list"  : str(self.nearLong2_ignore_list),
                "nearLong3_ignore_list"  : str(self.nearLong3_ignore_list),
                "nearLong4_ignore_list"  : str(self.nearLong4_ignore_list),
                "nearShort1_ignore_list" : str(self.nearShort1_ignore_list),
                "nearShort2_ignore_list" : str(self.nearShort2_ignore_list),
                "nearShort3_ignore_list" : str(self.nearShort3_ignore_list),
                "nearShort4_ignore_list" : str(self.nearShort4_ignore_list),
                }

            return res

    def resetHatikoInfo(self):        
        self.__init__(self.nMaxLong, self.nMaxShort, self.nIgnoreLong, self.nIgnoreShort)

        return "Reset HatikoInfo Complete!!!"
    
    def set_variable(self, variable: str, value: int):
        if variable == "nmax_long":
            self.nMaxLong = value
        elif variable == "nmax_short":
            self.nMaxShort = value
        elif variable == "nignore_long":
            self.nIgnoreLong = value
        elif variable == "nignore_short":
            self.nIgnoreShort = value
        elif variable == "liquidation_mdd":
            self.liquidationMDD = float(value)
        else:
            return "Wrong variable name!!!"
        return "Set " + variable + " to " + str(value) + "!!!"

    #endregion request 호출용 함수

    def countNearSignal(self) -> int:
        cnt = len(self.nearLong1_dic) + len(self.nearLong2_dic) + len(self.nearLong3_dic) + len(self.nearLong4_dic)
        return cnt

    def calcEntryRate(self, nMax: int, safetyMarginPercent: float=0) -> float:
        """
        nMax : nMaxLong
        safetyMarginPercent : Total자본금 * safetyMarginPercent/100 만큼은 안전마진으로 두고 쓰지 않는다

        진입비율 = entryCash / FreeCash
        return 진입비율
        """
        nNear = self.countNearSignal()
        nEnvelope = 4
        availableCashRate = 1 - safetyMarginPercent / 100
        entryRate = availableCashRate / (nEnvelope * nMax - nNear * availableCashRate)
        return entryRate
    
