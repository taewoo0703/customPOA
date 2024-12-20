import ccxt
import ccxt.async_support as ccxt_async
from devtools import debug

from exchange.model import MarketOrder
import exchange.error as error
from decimal import Decimal


class Okx:
    def __init__(self, key, secret, passphrase):
        self.client = ccxt.okx(
            {
                "apiKey": key,
                "secret": secret,
                "password": passphrase,
            }
        )
        self.client.load_markets()
        self.order_info: MarketOrder = None
        self.position_mode = "one-way"

    def init_info(self, order_info: MarketOrder):
        self.order_info = order_info

        unified_symbol = order_info.unified_symbol
        market = self.client.market(unified_symbol)

        is_contract = market.get("contract")
        if is_contract:
            order_info.is_contract = True
            order_info.contract_size = market.get("contractSize")

        if order_info.is_futures:
            self.client.options["defaultType"] = "swap"
        else:
            self.client.options["defaultType"] = "spot"

    def get_amount_precision(self, symbol):
        market = self.client.market(symbol)
        precision = market.get("precision")
        if (
            precision is not None
            and isinstance(precision, dict)
            and "amount" in precision
        ):
            return precision.get("amount")

    def get_contract_size(self, symbol):
        market = self.client.market(symbol)
        return market.get("contractSize")

    def parse_symbol(self, base: str, quote: str):
        if self.order_info.is_futures:
            return f"{base}/{quote}:{quote}"
        else:
            return f"{base}/{quote}"

    def get_ticker(self, symbol: str):
        return self.client.fetch_ticker(symbol)

    def get_price(self, symbol: str):
        return self.get_ticker(symbol)["last"]

    def get_balance(self, base: str):
        free_balance_by_base = None
        if self.order_info.is_entry or (
            self.order_info.is_spot
            and (self.order_info.is_buy or self.order_info.is_sell)
        ):
            free_balance = (
                self.client.fetch_free_balance()
                if not self.order_info.is_total
                else self.client.fetch_total_balance()
            )
            free_balance_by_base = free_balance.get(base)

        if free_balance_by_base is None or free_balance_by_base == 0:
            raise error.FreeAmountNoneError()
        return free_balance_by_base

    def get_futures_position(self, symbol=None, all=False):
        if symbol is None and all:
            positions = self.client.fetch_balance()["info"]["positions"]
            positions = [
                position
                for position in positions
                if float(position["positionAmt"]) != 0
            ]
            return positions

        positions = self.client.fetch_positions([symbol])
        long_contracts = None
        short_contracts = None
        if positions:
            for position in positions:
                if position["side"] == "long":
                    long_contracts = position["contracts"]
                elif position["side"] == "short":
                    short_contracts = position["contracts"]

            if self.order_info.is_close and self.order_info.is_buy:
                if not short_contracts:
                    raise error.ShortPositionNoneError()
                else:
                    return short_contracts
            elif self.order_info.is_close and self.order_info.is_sell:
                if not long_contracts:
                    raise error.LongPositionNoneError()
                else:
                    return long_contracts
        else:
            raise error.PositionNoneError()

    def get_amount(self, order_info: MarketOrder) -> float:
        if order_info.amount is not None and order_info.percent is not None:
            raise error.AmountPercentBothError()
        elif order_info.amount is not None:
            if order_info.is_contract:
                result = self.client.amount_to_precision(
                    order_info.unified_symbol,
                    float(
                        Decimal(str(order_info.amount))
                        // Decimal(str(order_info.contract_size))
                    ),
                )

            else:
                result = order_info.amount
        elif order_info.percent is not None:
            if self.order_info.is_entry or (order_info.is_spot and order_info.is_buy):
                if order_info.is_coinm:
                    free_base = self.get_balance(order_info.base)
                    if order_info.is_contract:
                        result = (
                            free_base * (order_info.percent - 0.5) / 100
                        ) // order_info.contract_size
                    else:
                        result = free_base * order_info.percent / 100
                else:
                    free_quote = self.get_balance(order_info.quote)
                    cash = free_quote * (order_info.percent - 0.5) / 100
                    current_price = self.get_price(order_info.unified_symbol)
                    if order_info.is_contract:
                        result = (cash / current_price) // order_info.contract_size
                    else:
                        result = cash / current_price
            elif self.order_info.is_close:
                if order_info.is_contract:
                    free_amount = self.get_futures_position(order_info.unified_symbol)
                    result = free_amount * order_info.percent / 100
                else:
                    free_amount = self.get_futures_position(order_info.unified_symbol)
                    result = free_amount * float(order_info.percent) / 100

            elif order_info.is_spot and order_info.is_sell:
                free_amount = self.get_balance(order_info.base)
                result = free_amount * float(order_info.percent) / 100

            result = float(
                self.client.amount_to_precision(order_info.unified_symbol, result)
            )
            order_info.amount_by_percent = result
        else:
            raise error.AmountPercentNoneError()

        return float(result)

    def market_order(self, order_info: MarketOrder):
        from exchange.pexchange import retry

        symbol = (
            order_info.unified_symbol
        )  # self.parse_symbol(order_info.base, order_info.quote)
        params = {"tgtCcy": "base_ccy"}

        try:
            return retry(
                self.client.create_order,
                symbol,
                order_info.type.lower(),
                order_info.side,
                order_info.amount,
                order_info.price,
                params,
                order_info=order_info,
                max_attempts=5,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    def market_buy(
        self,
        order_info: MarketOrder,
    ):
        # 수량기반
        buy_amount = self.get_amount(order_info)
        fee = self.client.fetch_trading_fee(self.order_info.unified_symbol)
        order_info.amount = buy_amount
        result = self.market_order(order_info)
        order_info.amount = buy_amount * (1 - fee["taker"])
        return result

    def market_sell(
        self,
        order_info: MarketOrder,
    ):
        # 수량기반
        symbol = (
            order_info.unified_symbol
        )  # self.parse_symbol(order_info.base, order_info.quote)
        fee = self.client.fetch_trading_fee(symbol)
        sell_amount = self.get_amount(order_info)

        if order_info.percent is not None:
            order_info.amount = sell_amount
        else:
            order_info.amount = sell_amount * (1 - fee["taker"])

        return self.market_order(order_info)

    def set_leverage(self, leverage, symbol):
        if self.order_info.is_futures:
            if self.order_info.is_futures and self.order_info.is_entry:
                if self.order_info.is_buy:
                    pos_side = "long"
                elif self.order_info.is_sell:
                    pos_side = "short"
            try:
                if (
                    self.order_info.margin_mode is None
                    or self.order_info.margin_mode == "isolated"
                ):
                    if self.position_mode == "hedge":
                        self.client.set_leverage(
                            leverage,
                            symbol,
                            params={"mgnMode": "isolated", "posSide": pos_side},
                        )
                    elif self.position_mode == "one-way":
                        self.client.set_leverage(
                            leverage,
                            symbol,
                            params={"mgnMode": "isolated", "posSide": "net"},
                        )
                else:
                    self.client.set_leverage(
                        leverage,
                        symbol,
                        params={"mgnMode": self.order_info.margin_mode},
                    )
            except Exception as e:
                pass

    def market_entry(
        self,
        order_info: MarketOrder,
    ):
        from exchange.pexchange import retry

        symbol = (
            order_info.unified_symbol
        )  # self.parse_symbol(order_info.base, order_info.quote)

        entry_amount = self.get_amount(order_info)
        if entry_amount == 0:
            raise error.MinAmountError()

        params = {}
        if order_info.leverage is None:
            self.set_leverage(1, symbol)
        else:
            self.set_leverage(order_info.leverage, symbol)
        if order_info.margin_mode is None:
            params |= {"tdMode": "isolated"}
        else:
            params |= {"tdMode": order_info.margin_mode}

        if self.position_mode == "one-way":
            params |= {}
        elif self.position_mode == "hedge":
            if order_info.is_futures and order_info.side == "buy":
                if order_info.is_entry:
                    pos_side = "long"
                elif order_info.is_close:
                    pos_side = "short"
            elif order_info.is_futures and order_info.side == "sell":
                if order_info.is_entry:
                    pos_side = "short"
                elif order_info.is_close:
                    pos_side = "long"
            params |= {"posSide": pos_side}

        try:
            return retry(
                self.client.create_order,
                symbol,
                order_info.type.lower(),
                order_info.side,
                abs(entry_amount),
                None,
                params,
                order_info=order_info,
                max_attempts=5,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    def market_close(
        self,
        order_info: MarketOrder,
    ):
        from exchange.pexchange import retry

        symbol = self.order_info.unified_symbol
        close_amount = self.get_amount(order_info)

        if self.position_mode == "one-way":
            if (
                self.order_info.margin_mode is None
                or self.order_info.margin_mode == "isolated"
            ):
                params = {"reduceOnly": True, "tdMode": "isolated"}
            elif self.order_info.margin_mode == "cross":
                params = {"reduceOnly": True, "tdMode": "cross"}

        elif self.position_mode == "hedge":
            if order_info.is_futures and order_info.side == "buy":
                if order_info.is_entry:
                    pos_side = "long"
                elif order_info.is_close:
                    pos_side = "short"
            elif order_info.is_futures and order_info.side == "sell":
                if order_info.is_entry:
                    pos_side = "short"
                elif order_info.is_close:
                    pos_side = "long"
            if (
                self.order_info.margin_mode is None
                or self.order_info.margin_mode == "isolated"
            ):
                params = {"posSide": pos_side, "tdMode": "isolated"}
            elif self.order_info.margin_mode == "cross":
                params = {"posSide": pos_side, "tdMode": "cross"}

        try:
            return retry(
                self.client.create_order,
                symbol,
                order_info.type.lower(),
                order_info.side,
                abs(close_amount),
                None,
                params,
                order_info=order_info,
                max_attempts=5,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)


##############################################################################
# by PTW
##############################################################################

    # hatiko용 get_amount
    def get_amount_hatiko(self, symbol, nMaxLong, nMaxShort, entryRate: float=0, liquidationMDD: float=80.0) -> float:
        """
        entryRate : 현물인 경우에만 사용함. entryCash = entryRate * freecash
        """
        # 선물 일 때
        if self.order_info.is_futures:
            # Long Entry
            if self.order_info.is_entry and self.order_info.side in ("buy"):
                total_bal = float(self.client.fetch_balance().get('total').get('USDT'))
                cash = total_bal / 4.0 / nMaxLong     # 총 자본을 4분할 + nMaxLong종목 몰빵
                cash = cash * 100.0 / liquidationMDD  # 청산당할 MDD 고려
                result = cash / self.order_info.price
                if self.order_info.is_contract:
                    result = result // self.order_info.contract_size

            # Short Entry
            if self.order_info.is_entry and self.order_info.side in ("sell"):
                total_bal = float(self.client.fetch_balance().get('total').get('USDT'))
                cash = total_bal / 4.0 / nMaxShort    # 총 자본을 4분할 + nMaxShort종목 몰빵
                cash = cash * 100.0 / 150.0  # 청산당할 MDD를 150%로 설정하기 때문에 100/150을 곱함.
                result = cash / self.order_info.price
                if self.order_info.is_contract:
                    result = result // self.order_info.contract_size

            # Long Exit & Short Exit
            if self.order_info.is_close:
                symbol = self.order_info.unified_symbol
                free_amount = self.get_futures_position_hatiko(symbol)
                result = free_amount        # 팔 때는 100% 전량 매도함

        # 현물 일 때
        if self.order_info.is_spot:
            # Buy
            if self.order_info.side in ("buy"):
                if self.order_info.amount is not None:
                    result = self.order_info.amount
                else:
                    free_quote = self.get_balance_hatiko(self.order_info.quote)
                    cash = free_quote * entryRate
                    result = cash / self.order_info.price

            # Sell
            if self.order_info.side in ("sell"):
                free_amount = self.get_balance_hatiko(self.order_info.base)
                result = free_amount

        return result

    # hatiko용 get_balance
    # "거래할 수량이 없습니다" Error를 발생시키지 않음.
    # 나머지는 동일
    def get_balance_hatiko(self, base: str):
        free_balance_by_base = None
        if self.order_info.is_entry or (
            self.order_info.is_spot and (self.order_info.is_buy or self.order_info.is_sell)
        ):
            free_balance = self.client.fetch_free_balance()
            free_balance_by_base = free_balance.get(base)

        if free_balance_by_base is None or free_balance_by_base == 0:
            free_balance_by_base = 0
            # raise error.FreeAmountNoneError()
            
        return free_balance_by_base

    # hatiko용 get_futures_position
    # "거래할 수량이 없습니다" Error를 발생시키지 않음.
    # 나머지는 동일
    def get_futures_position_hatiko(self, symbol=None, all=False):
        if symbol is None and all:
            positions = self.client.fetch_balance()["info"]["positions"]
            positions = [position for position in positions if float(position["positionAmt"]) != 0]
            return positions

        positions = self.client.fetch_positions([symbol])
        long_contracts = 0
        short_contracts = 0
        if positions:
            for position in positions:
                if position["side"] == "long":
                    long_contracts = position["contracts"]
                elif position["side"] == "short":
                    short_contracts = position["contracts"]

            if self.order_info.is_close and self.order_info.is_buy:
                # if not short_contracts:
                #     raise error.ShortPositionNoneError()
                # else:
                return short_contracts
            elif self.order_info.is_close and self.order_info.is_sell:
                # if not long_contracts:
                #     raise error.LongPositionNoneError()
                # else:
                return long_contracts
        else:
            # raise error.PositionNoneError()
            return 0
        
    # limit 오더 함수
    # market_order와 market_entry, market_close 함수를 최대한 활용함 (market_close와 겸용으로 사용)
    # hedge 모드 관련 코드 모두 삭제, 
    # 마진모드는 cross를 기본으로 지향한다.
    def limit_order(self, order_info: MarketOrder, amount: float, price: float):   
        from exchange.pexchange import retry

        symbol = order_info.unified_symbol  # self.parse_symbol(order_info.base, order_info.quote)
        params = {}
        if order_info.is_futures :
            margin_mode = order_info.margin_mode if order_info.margin_mode is not None else "cross"
            params |= {"tdMode": margin_mode}
            if order_info.is_entry:
                if order_info.leverage is not None:
                    self.set_leverage(order_info.leverage, symbol)
            elif order_info.is_close:
                if self.position_mode == "one-way":
                    params |= {"reduceOnly": True}

        try:
            return retry(
                self.client.create_order,
                symbol,
                "limit",
                order_info.side,
                amount,
                price,
                params,
                order_info=order_info,
                max_attempts=5,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    # fetch_order (거래소에 따라 params 다름)
    def fetch_order(self, order_id: str, symbol: str):
        return self.client.fetch_order(order_id, symbol, params={"acknowledged": True})