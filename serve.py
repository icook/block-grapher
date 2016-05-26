import datetime
import sys
import time
import logging
import sqlalchemy.exc
from decimal import Decimal
from bitcoin.rpc import Proxy
from flask import Flask, jsonify, render_template, session, redirect, url_for, Response, stream_with_context
from flask.ext.sqlalchemy import SQLAlchemy
import config

app = Flask(__name__)
app.secret_key = "tsdkfljglkjsdfg"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///index.db'
proxies = {}
for conf in config.proxy_addresses:
    p = Proxy(conf['address'])
    p.name = conf['name']
    proxies[conf['name']] = p

block_cache = {}
# We store needed block information that is an ephemeral cache
db = SQLAlchemy(app)


class Block(db.Model):
    height = db.Column(db.Integer, primary_key=True)
    currency = db.Column(db.Integer, primary_key=True)
    difficulty = db.Column(db.Float)
    time = db.Column(db.DateTime)
    subsidy = db.Column(db.Numeric)

    @property
    def hashes_required(self):
        return self.difficulty * 2**256 / (0xffff * 2**208)

    @property
    def nTime(self):
        return time.mktime(self.time.timetuple())


@app.before_first_request
def setup_logging():
    if not app.debug:
        # In production mode, add log handler to sys.stderr.
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler = logging.StreamHandler()
        app.logger.addHandler(handler)
        app.logger.setLevel(logging.INFO)
        handler.setFormatter(formatter)


@app.route('/')
def home():
    coins = []
    for proxy in proxies.values():
        coins.append((proxy, proxy.getinfo()))
    return render_template("home.html", coins=coins, now=int(time.time()))


@app.route('/graph/<currency>/<start>/<stop>/')
def graph(currency, start, stop):
    proxy = proxies[currency]
    start = datetime.datetime.utcfromtimestamp(float(start))
    stop = datetime.datetime.utcfromtimestamp(float(stop))
    block_objs = (Block.query.filter_by(currency=currency).
                  filter(Block.time > start).
                  filter(Block.time < stop).
                  order_by(Block.time.desc()).
                  limit(2000))
    blocks = [dict(difficulty=block.difficulty,
                   height=block.height,
                   time=block.nTime,
                   subsidy=float(block.subsidy),
                   hashes_required=block.hashes_required) for block in block_objs]


    return render_template("graph.html", blocks=blocks, start=start, proxy=proxy)


@app.route('/sync')
def sync():
    return Response(stream_with_context(sync_db()), mimetype='text/plain')

def sync_db(proxies_to_sync=None, max_sync_number=None):
    app.logger.info("Starting sync")
    db.create_all()

    # Default to syncing all configured proxies
    if proxies_to_sync is None:
        proxies_to_sync = proxies.values()

    for proxy in proxies_to_sync:
        info = proxy.getinfo()
        last_block = Block.query.filter_by(currency=proxy.name).order_by(Block.height.desc()).first()
        last_sync_height = last_block.height if last_block else 0

        # We're already at the latest block, no need to sync
        if info['blocks'] <= last_sync_height:
            continue

        app.logger.info("Starting sync for {}. {} blocks to sync".format(proxy.name, info['blocks'] - last_sync_height))

        # If we have to sync for too long abort trying to render the page. It
        # will be an unnaceptable delay
        if max_sync_number is not None and info['blocks'] - last_sync_height > max_sync_number:
            abort(401)

        next_blockhash = None
        for i in range(last_sync_height + 1, info['blocks']):
            # If we found a next blockhash...
            if next_blockhash:
                blockhash = next_blockhash
            else:
                blockhash = proxy.getblockhash(i)

            block = proxy.getblock(blockhash)
            subsidy = 0

            for tx in block.vtx:
                for _, txout in enumerate(tx.vout):
                    out_dec = Decimal(txout.nValue) / 100000000
                    if tx.is_coinbase():
                        subsidy += out_dec

            block_obj = Block(
                difficulty=block.difficulty,
                height=i,
                currency=proxy.name,
                subsidy=subsidy,
                time=datetime.datetime.utcfromtimestamp(block.nTime))
            db.session.add(block_obj)

            try:
                db.session.commit()
            except (sqlalchemy.exc.IntegrityError, sqlalchemy.orm.exc.FlushError):
                db.session.rollback()

            percent_complete = (i - last_sync_height) / (info['blocks'] - last_sync_height) * 100
            if i % 100 == 0:
                msg = "{:,}/{:,} ({:,.2f}) Synced {} {:,}\n".format(i, info['blocks'], percent_complete, block_obj.currency, block_obj.height)
                yield msg
                app.logger.warn(msg)
        yield "sync of {} complete!\n".format(proxy.name)

if __name__ == '__main__':
    if sys.argv[1] == "sync":
        with app.app_context():
            for msg in sync_db():
                sys.stdout.write(msg)

    app.run(debug=True)
