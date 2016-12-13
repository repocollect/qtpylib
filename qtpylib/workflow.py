#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# QTPyLib: Quantitative Trading Python Library
# https://github.com/ranaroussi/qtpylib
#
# Copyright 2016 Ran Aroussi
#
# Licensed under the GNU Lesser General Public License, v3.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.gnu.org/licenses/lgpl-3.0.en.html
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import numpy as np
import pandas as pd
import pymysql
import requests
import logging

from io import StringIO
from pandas_datareader import data as web

from qtpylib import tools

from qtpylib.blotter import (
    load_blotter_args, get_symbol_id,
    mysql_insert_tick, mysql_insert_bar
)

from ezibpy import ezIBpy

# =============================================
tools.createLogger(__name__)
# =============================================

def get_data_yahoo(symbols, start, end=None, *args, **kwargs):
    """
    Downloads and auto-adjusts daily data from Yahoo finance

    :Parameters:
        symbols : str/list
            symbol(s) to downlaod intraday data for
        start : str
            Earliest date to download

    :Optional:
        end : str
            Latest date to download (default: today)

    :Returns:
        data : pd.DataFrame
            Pandas DataFrame with 1-minute bar data
    """

    dfs = []

    # list of symbols?
    if not isinstance(symbols, list):
        symbols = [symbols]

    # get the data
    data = web.get_data_yahoo(symbols, start, end)

    # parse the data
    ohlc = []
    for sym in symbols:
        ohlc = pd.DataFrame({
            "open": data['Open'][sym],
            "high": data['High'][sym],
            "low": data['Low'][sym],
            "close": data['Close'][sym],
            "adj_close": data['Adj Close'][sym],
            "volume": data['Volume'][sym]
        })
        ohlc.index.names = ['date']

        # auto-adjust prices
        ratio = ohlc["close"]/ohlc["adj_close"]
        ohlc["adj_open"] = ohlc["open"]/ratio
        ohlc["adj_high"] = ohlc["high"]/ratio
        ohlc["adj_low"]  = ohlc["low"]/ratio

        ohlc.drop(["open","high","low","close"], axis=1, inplace=True)
        ohlc.rename(columns={
            "adj_open": "open",
            "adj_high": "high",
            "adj_low": "low",
            "adj_close": "close"
        }, inplace=True)

        # round
        decimals = pd.Series([2, 2, 2, 2], index=['open', 'high', 'low', 'close'])
        ohlc = ohlc.round(decimals)

        # re-order columns
        ohlc.loc[:, "symbol"] = sym
        ohlc = ohlc[['symbol', 'open', 'high', 'low', 'close', 'volume']]
        ohlc['volume'] = ohlc['volume'].astype(int)

        # add to collection
        dfs.append(ohlc)

    return pd.concat(dfs).sort_index()


def get_data_yahoo_intraday(symbol, *args, **kwargs):
    """
    Import intraday data (1M) from Yahoo finance (2 weeks max)

    :Parameters:
        symbol : str
            symbol to downlaod intraday data for
    :Returns:
        data : pd.DataFrame
            Pandas DataFrame with 1-minute bar data
    """
    raw = requests.get("http://chartapi.finance.yahoo.com/instrument/1.0/"+symbol+"/chartdata;type=quote;range=10d/csv")

    cols = raw.text.split("values:")[1].split("\n")[0].lower()
    data = raw.text.split("volume:")[1].split("\n")
    data = "\n".join(data[1:])

    if "timestamp" in cols:
        df = pd.read_csv(StringIO(cols+"\n"+data), index_col=["timestamp"], parse_dates=["timestamp"])
        df.index = pd.to_datetime(df.index, unit='s')

        timezone = raw.text.split("timezone:")[1].split("\n")[0]
        df = tools.set_timezone(df, timezone)

        df = df.resample("T").last().dropna()
        df['volume'] = df['volume'].astype(int)
        return np.round(df, 2)

    # parse csv
    df = pd.read_csv(StringIO(cols+"\n"+data), index_col=["date"], parse_dates=["date"])
    df.index = pd.to_datetime(df.index, utc=True)

    # round
    decimals = pd.Series([2, 2, 2, 2], index=['open', 'high', 'low', 'close'])
    df = df.round(decimals)

    df.loc[:, "symbol"] = symbol
    return df[["symbol", "open", "high", "low", "close", "volume"]]


def get_data_google_intraday(symbol, *args, **kwargs):
    """
    Import intraday data (1M) from Google finance (3 weeks max)

    :Parameters:
        symbol : str
            symbol to downlaod intraday data for
    :Returns:
        data : pd.DataFrame
            Pandas DataFrame with 1-minute bar data
    """

    # build request url
    url = "https://www.google.com/finance/getprices?q="+symbol+"&i=60&p=30d&f=d,o,h,l,c,v"
    for arg in kwargs:
        url += "&"+ arg +"="+ kwargs[arg]

    # get data
    raw = requests.get(url)

    # split newlines
    data = raw.text.split("\n")

    # read to dataframe
    df = pd.read_csv(StringIO("\n".join(data[7:])), names=["offset","open","high","low","close","volume"])

    # adjust offset
    df['timestamp'] = df[df['offset'].str[0] == "a"]['offset'].str[1:].astype(int)
    df.loc[df[df['timestamp'].isnull()==False].index, 'offset'] = 0
    df['timestamp'] = df['timestamp'].ffill() + df['offset'].astype(int) * 60

    # convert to datetime
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')

    # set index
    df.set_index(["timestamp"], inplace=True)

    # round
    decimals = pd.Series([2, 2, 2, 2], index=['open', 'high', 'low', 'close'])
    df = df.round(decimals)

    # return df
    df.loc[:, "symbol"] = symbol
    return df[["symbol", "open", "high", "low", "close", "volume"]]


_bars_colsmap = {
    'open': 'open',
    'high': 'high',
    'low': 'low',
    'close': 'close',
    'volume': 'volume',
    'opt_price': 'opt_price',
    'opt_underlying': 'opt_underlying',
    'opt_dividend': 'opt_dividend',
    'opt_volume': 'opt_volume',
    'opt_iv': 'opt_iv',
    'opt_oi': 'opt_oi',
    'opt_delta': 'opt_delta',
    'opt_gamma': 'opt_gamma',
    'opt_vega': 'opt_vega',
    'opt_theta': 'opt_theta'
}
_ticks_colsmap = {
    'bid': 'bid',
    'bidsize': 'bidsize',
    'ask': 'ask',
    'asksize': 'asksize',
    'last': 'last',
    'lastsize': 'lastsize',
    'opt_price': 'opt_price',
    'opt_underlying': 'opt_underlying',
    'opt_dividend': 'opt_dividend',
    'opt_volume': 'opt_volume',
    'opt_iv': 'opt_iv',
    'opt_oi': 'opt_oi',
    'opt_delta': 'opt_delta',
    'opt_gamma': 'opt_gamma',
    'opt_vega': 'opt_vega',
    'opt_theta': 'opt_theta'
}

def validate_columns(df, kind="BAR"):
    global _bars_colsmap, _ticks_colsmap

    # validate columns
    if "asset_class" not in df.columns:
        raise ValueError('Column asset_class not found')
        return False

    is_option = "OPT" in list(df['asset_class'].unique())

    colsmap = _ticks_colsmap if kind=="TICK" else _bars_colsmap

    for el in colsmap:
        col = colsmap[el]
        if col not in df.columns:
            if "opt_" in col and is_option:
                raise ValueError('Column %s not found' % el)
                return False
            elif "opt_" not in col and not is_option:
                raise ValueError('Column %s not found' % el)
                return False
    return True


def prepare_data(instrument, df, output_path=None, index=None, colsmap=None, kind="BAR"):
    """
    Converts given DataFrame to a QTPyLib-compatible format and timezone

    :Parameters:
        instrument : mixed
            IB contract tuple / string (same as that given to strategy)
        df : pd.DataFrame
            Pandas DataDrame with that instrument's market data
        output_path : str
            Path to the location where the resulting CSV should be saved (default: ``None``)
        index : pd.Series
            Pandas Series that will be used for df's index (default is to use df.index)
        colsmap : dict
            Dict for mapping df's columns to those used by QTPyLib (default assumes same naming convention as QTPyLib's)
        kind : str
            Is this ``BAR`` or ``TICK`` data

    :Returns:
        data : pd.DataFrame
            Pandas DataFrame in a QTPyLib-compatible format and timezone
    """

    global _bars_colsmap, _ticks_colsmap

    # work on copy
    df = df.copy()

    # set index
    if index is None:
        index = df.index

    # set defaults columns
    if not isinstance(colsmap, dict):
        colsmap = {}

    _colsmap = _ticks_colsmap if kind=="TICK" else _bars_colsmap
    for el in _colsmap:
        if el not in colsmap:
            colsmap[el] = _colsmap[el]

    # generate a valid ib tuple
    instrument = tools.create_ib_tuple(instrument)

    # create contract string (no need for connection)
    ibConn = ezIBpy()
    contract_string = ibConn.contractString(instrument)
    asset_class = tools.gen_asset_class(contract_string)
    symbol_group = tools.gen_symbol_group(contract_string)

    # add symbol data
    df.loc[:, 'symbol'] = contract_string
    df.loc[:, 'symbol_group'] = symbol_group
    df.loc[:, 'asset_class'] = asset_class

    # validate columns
    valid_cols = validate_columns(df, kind)
    if not valid_cols:
        raise ValueError('Invalid Column list')

    # rename columns to map
    df.rename(columns=colsmap, inplace=True)

    # force option columns on options
    if asset_class == "OPT":
        df = tools.force_options_columns(df)

    # remove all other columns
    known_cols = list(colsmap.values())+['symbol','symbol_group','asset_class']
    for col in df.columns:
        if col not in known_cols:
            df.drop(col, axis=1, inplace=True)

    # set UTC index
    df.index = pd.to_datetime(index)
    df = tools.set_timezone(df, "UTC")
    df.index.rename("datetime", inplace=True)

    # save csv
    if output_path is not None:
        output_path = output_path[:-1] if output_path.endswith('/') else output_path
        df.to_csv(output_path +"/"+ contract_string + ".csv")

    # return df
    return df


def store_data(df, blotter=None, kind="BAR"):
    """
    Store QTPyLib-compatible csv files in Blotter's MySQL.
    TWS/GW data are required for determining futures/options expiration

    :Parameters:
        df : dict
            Tick/Bar data

    :Optional:
        blotter : str
            Store MySQL server used by this Blotter (default is "auto detect")
        kind : str
            Is this ``BAR`` or ``TICK`` data
    """

    # validate columns
    valid_cols = validate_columns(df, kind)
    if not valid_cols:
        raise ValueError('Invalid Column list')

    # load blotter settings
    blotter_args = load_blotter_args(blotter, logger=logging.getLogger(__name__))

    # blotter not running
    if blotter_args is None:
        raise Exception("Cannot connect to running Blotter.")
        return False

    # cannot continue
    if blotter_args['dbskip']:
        raise Exception("Cannot continue. Blotter running with --skipdb")
        return False

    # connect to mysql using blotter's settings
    dbconn = pymysql.connect(
        host   = str(blotter_args['dbhost']),
        port   = int(blotter_args['dbport']),
        user   = str(blotter_args['dbuser']),
        passwd = str(blotter_args['dbpass']),
        db     = str(blotter_args['dbname']),
        autocommit = True
    )
    dbcurr = dbconn.cursor()

    # loop through symbols and save in db
    for symbol in list(df['symbol'].unique()):
        data = df[df['symbol'] == symbol]
        symbol_id = get_symbol_id(symbol, dbconn, dbcurr)

        # prepare columns for insert
        data.loc[:, 'timestamp'] = data.index.strftime('%Y-%m-%d %H:%M:%S')
        data.loc[:, 'symbol_id'] = symbol_id

        # insert row by row to handle greeks
        data = data.to_dict(orient="records")

        if kind == "BAR":
            for _, row in enumerate(data):
                mysql_insert_bar(row, symbol_id, dbcurr)
        else:
            for _, row in enumerate(data):
                mysql_insert_tick(row, symbol_id, dbcurr)

        try:
            dbconn.commit()
        except:
            return False

    return True


def analyze_portfolio(file):
    """ analyze portfolio (TBD) """
    pass

