import psycopg2
import time
from datetime import datetime
from collections import deque

from .helpers import Random, TPCCText, TimestampGenerator
from .helpers import MAX_ITEMS, DIST_PER_WARE, CUST_PER_DIST, NUM_ORDERS, STOCKS, NAMES

class Transactions:
    def __init__(self, seed, scale_factor, latest_timestamp, conn, dry_run):
        self.conn = conn
        self.random = Random(seed)
        self.tpcc_text = TPCCText(self.random)
        self.scale_factor = scale_factor
        self.dry_run = dry_run

        # the loader only generates timestamps for the orders table, and
        # generates a timestamp stream per warehouse.
        # here we generate a tsx for any warehouse and therefore have to scale
        # for both: 10/23 and scale_factor. the 10/23 comes from next_transaction
        # and is the ratio between calls to new_order() and timestamp_generator.next()
        timestamp_scalar = (10/23.0) / self.scale_factor

        self.timestamp_generator = TimestampGenerator(
                latest_timestamp, self.random, timestamp_scalar
        )
        self.ok_count = 0
        self.err_count = 0
        self.new_order_count = 0
        self.query_stats = deque()

    def add_stats(self, query, state, start):
        now = time.time()
        self.query_stats.append({'timestamp': now, 'query': query, 'runtime': now - start})
        # append the state as a query type too to ease showing it in the monitor
        self.query_stats.append({'timestamp': now, 'query': state, 'runtime': now - start})

    def stats(self):
        query_stats = self.query_stats
        self.query_stats = deque()
        return query_stats

    def other_ware(self, home_ware):
        if self.scale_factor == 1:
            return home_ware

        while True:
            tmp = self.random.randint_inclusive(1, self.scale_factor)
            if tmp != home_ware:
                return tmp

    def execute_sql(self, sql, args, query_type):
        if self.dry_run:
            return;
        start = time.time()
        try:
            self.conn.cursor.execute(sql, args)
            self.add_stats(query_type, 'ok', start)
        # do not catch timeouts because we want that to stop the benchmark.
        # if we get timeouts the benchmark gets inbalanced and we eventually get
        # to a complete halt.
        except psycopg2.errors.RaiseException as err:
            if 'Item record is null' in err.pgerror:
                self.add_stats(query_type, 'error', start)
                pass
            else:
                raise

    def new_order(self, timestamp):
        w_id = self.random.randint_inclusive(1, self.scale_factor)
        d_id = self.random.randint_inclusive(1, DIST_PER_WARE)
        c_id = self.random.nurand(1023, 1, CUST_PER_DIST)
        order_line_count = self.random.randint_inclusive(5, 15)
        rbk = self.random.randint_inclusive(1, 100)
        itemid = []
        supware = []
        qty = []
        all_local = 1

        for order_line in range(1, order_line_count + 1):
            itemid.append(self.random.nurand(8191, 1, MAX_ITEMS))
            if (order_line == order_line_count - 1) and (rbk == 1):
                itemid[-1] = -1

            if self.random.randint_inclusive(1, 100) != 1:
                supware.append(w_id)
            else:
                supware.append(self.other_ware(w_id))
                all_local = 0

            qty.append(self.random.randint_inclusive(1, 10))

        sql = 'SELECT new_order(%s, %s, %s, %s, %s, %s, %s, %s, %s)'
        args = (w_id, c_id, d_id, order_line_count, all_local, itemid, supware, qty, timestamp)
        # rolled back or commit tsxs they both count
        self.new_order_count += 1
        self.execute_sql(sql, args, 'new_order')

    def payment(self, timestamp):
        w_id = self.random.randint_inclusive(1, self.scale_factor)
        d_id = self.random.randint_inclusive(1, DIST_PER_WARE)
        c_id = self.random.nurand(1023, 1, CUST_PER_DIST)
        h_amount = self.random.randint_inclusive(1, 5000)
        c_last = self.tpcc_text.lastname(self.random.nurand(255, 0, 999))

        byname = self.random.randint_inclusive(1, 100) <= 60
        if self.random.randint_inclusive(1, 100) <= 85:
            c_w_id = w_id
            c_d_id = d_id
        else:
            c_w_id = self.other_ware(w_id)
            c_d_id = self.random.randint_inclusive(1, DIST_PER_WARE)

        sql = 'SELECT payment(%s, %s, %s, %s, %s, %s, %s, %s, %s)'
        args = (w_id, d_id, c_d_id, c_id, c_w_id, h_amount, byname, c_last, timestamp)
        self.execute_sql(sql, args, 'payment')

    def order_status(self):
        w_id = self.random.randint_inclusive(1, self.scale_factor)
        d_id = self.random.randint_inclusive(1, DIST_PER_WARE)
        c_id = self.random.nurand(1023, 1, CUST_PER_DIST)
        c_last = self.tpcc_text.lastname(self.random.nurand(255, 0, 999))
        byname = self.random.randint_inclusive(1, 100) <= 60

        sql = 'SELECT * FROM order_status(%s, %s, %s, %s, %s)'
        args = (w_id, d_id, c_id, c_last, byname)
        self.execute_sql(sql, args, 'order_status')

    def delivery(self, timestamp):
        w_id = self.random.randint_inclusive(1, self.scale_factor)
        o_carrier_id = self.random.randint_inclusive(1, 10)

        sql = 'SELECT * FROM delivery(%s, %s, %s, %s)'
        args = (w_id, o_carrier_id, DIST_PER_WARE, timestamp)
        self.execute_sql(sql, args, 'delivery')

    def stock_level(self):
        w_id = self.random.randint_inclusive(1, self.scale_factor)
        d_id = self.random.randint_inclusive(1, DIST_PER_WARE)
        level = self.random.randint_inclusive(10, 20)

        sql = 'SELECT * FROM stock_level(%s, %s, %s)'
        args = (w_id, d_id, level)
        self.execute_sql(sql, args, 'stock_level')

    def next_transaction(self):
        timestamp_to_use = self.timestamp_generator.next()

        # WARNING: keep in sync with initialization of scalar of timestamp generator!
        trx_type = self.random.randint_inclusive(1, 23)
        if trx_type <= 10:
                self.new_order(timestamp_to_use)
        elif trx_type <= 20:
                self.payment(timestamp_to_use)
        elif trx_type <= 21:
                self.order_status()
        elif trx_type <= 22:
                self.delivery(timestamp_to_use)
        elif trx_type <= 23:
                self.stock_level()

        
