import datetime
import sys
import time
import ago
import logging
import sqlalchemy.exc
from decimal import Decimal
from bitcoin.rpc import Proxy
from flask import Flask, jsonify, render_template, session, redirect, url_for, Response, stream_with_context
from flask.ext.sqlalchemy import SQLAlchemy
import config

app = Flask(__name__, static_url_path='/static')
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


@app.template_filter('human_date')
def human_date_utc(*args, **kwargs):
    if isinstance(args[0], type(None)):
        return "never"
    if isinstance(args[0], (int, float, str)):
        args = [datetime.datetime.utcfromtimestamp(float(args[0]))] + list(args[1:])
    delta = (datetime.datetime.utcnow() - args[0])
    delta = delta - datetime.timedelta(microseconds=delta.microseconds)
    result = ago.human(delta, *args[1:], **kwargs)
    return "just now" if result == " ago" else result


@app.template_filter('duration')
def duration(seconds):
    # microseconds
    if seconds > 7776000:
        return "{}".format(datetime.timedelta(seconds=seconds))
    if seconds >= 86400:
        return "{:,.1f} days".format(seconds / 86400)
    if seconds >= 3600:
        return "{:,.1f} hours".format(seconds / 3600)
    if seconds >= 60:
        return "{:,.2f} mins".format(seconds / 60.0)
    if seconds <= 1.0e-3:
        return "{:,.4f} us".format(seconds * 1000000.0)
    if seconds <= 1.0:
        return "{:,.4f} ms".format(seconds * 1000.0)
    return "{:,.4f} sec".format(seconds)


class Block(db.Model):
    height = db.Column(db.Integer, primary_key=True)
    currency = db.Column(db.Integer, primary_key=True)
    difficulty = db.Column(db.Float)
    time = db.Column(db.DateTime)
    subsidy = db.Column(db.Numeric)
    last_fifteen = db.Column(db.Float)

    @property
    def hashes_required(self):
        return self.difficulty * 2**256 / (0xffff * 2**208)

    @property
    def nTime(self):
        return int(time.mktime(self.time.timetuple()))

    def __str__(self):
        return "<{} Block height {:,}>".format(self.currency, self.height)

    def __repr__(self):
        return "<{} Block height {:,}>".format(self.currency, self.height)


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
        last_block = Block.query.filter_by(currency=proxy.name).order_by(Block.time.desc()).first()
        coins.append((proxy, last_block))
    return render_template("home.html", coins=coins, now=int(time.time()))


@app.route('/graph/<currency>/<int:start>/<int:stop>/')
def graph(currency, start, stop):
    proxy = proxies[currency]
    start_dt = datetime.datetime.utcfromtimestamp(start)
    stop_dt = datetime.datetime.utcfromtimestamp(stop)
    block_objs = (Block.query.filter_by(currency=currency).
                  filter(Block.time > start_dt).
                  filter(Block.time < stop_dt).
                  order_by(Block.time.desc()).
                  limit(2000))
    blocks = [dict(difficulty=block.difficulty,
                   height=block.height,
                   time=block.nTime,
                   subsidy=float(block.subsidy),
                   hashes_required=block.hashes_required,
                   last_fifteen=block.last_fifteen) for block in block_objs]


    return render_template("graph.html",
                           blocks=blocks,
                           start=start,
                           stop=stop,
                           start_dt=start_dt,
                           stop_dt=stop_dt,
                           proxy=proxy)


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
        if last_block:
            last_blocks = Block.query.filter_by(currency=proxy.name).order_by(Block.height.desc()).filter(Block.height >= last_block.height - 15).all()
        else:
            last_blocks = []
        last_sync_height = last_block.height if last_block else 0

        # We're already at the latest block, no need to sync
        if info['blocks'] <= last_sync_height:
            yield "sync of {} unneeded\n".format(proxy.name)
            continue

        app.logger.info("Starting sync for {}. {} blocks to sync".format(proxy.name, info['blocks'] - last_sync_height))

        # If we have to sync for too long abort trying to render the page. It
        # will be an unnaceptable delay
        if max_sync_number is not None and info['blocks'] - last_sync_height > max_sync_number:
            abort(401)

        next_blockhash = None
        start = time.time()
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
            if len(last_blocks) >= 16:
                last_blocks.pop()
            last_blocks.insert(0, block_obj)
            if len(last_blocks) >= 16:
                total_hashes = sum([b.hashes_required for b in last_blocks[1:]])
                block_obj.last_fifteen = float(total_hashes) / (last_blocks[0].time - last_blocks[-1].time).total_seconds()
            db.session.add(block_obj)

            if i % 100 == 0:
                percent_complete = (i - last_sync_height) / (info['blocks'] - last_sync_height) * 100
                time_estimate = ((time.time() - start) / (i - last_sync_height)) * (info['blocks'] - i) / 60
                msg = "{:,}/{:,} ({:,.2f}) Synced {} {:,}. Est {:,.2f} minutes left\n".format(
                    i, info['blocks'], percent_complete, block_obj.currency, block_obj.height, time_estimate)
                yield msg
                app.logger.info(msg)

                try:
                    db.session.commit()
                except (sqlalchemy.exc.IntegrityError, sqlalchemy.orm.exc.FlushError):
                    app.logger.exception("Failed to commit new block")
                    db.session.rollback()
        db.session.commit()

        yield "sync of {} complete!\n".format(proxy.name)

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        with app.app_context():
            for msg in sync_db():
                sys.stdout.write(msg)
        exit()

    app.run(debug=True)
