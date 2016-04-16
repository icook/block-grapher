import json
from decimal import Decimal
from bitcoin.rpc import Proxy
from flask import Flask, jsonify, render_template, session, redirect, url_for
import config

app = Flask(__name__)
app.secret_key = "tsdkfljglkjsdfg"
proxies = []
for conf in config.proxy_addresses:
    p = Proxy(conf['address'])
    p.name = conf['name']
    proxies.append(p)

block_cache = {}


def fetch_proxy():
    try:
        proxy = proxies[session.get('proxy', 0)]
    except IndexError:
        proxy = proxies[0]
        session['proxy'] = 0

    return proxy


@app.route('/change_proxy/<int:idx>')
def change_proxy(idx):
    session['proxy'] = idx
    return redirect(url_for('home'))


@app.route('/')
def home():
    proxy = fetch_proxy()
    info = proxy.getinfo()
    return render_template("home.html", info=info, proxies=proxies, proxy=proxy)


@app.route('/graph/<start>/<step>/')
def graph(start, step):
    proxy = fetch_proxy()
    step = int(step)
    if step > 2000:
        step = 2000
    start = int(start)
    end = start + step
    blocks = []
    for i in range(start, end):
        if i in block_cache:
            blocks.append(block_cache["{}_{}".format(proxy.name, i)])
            continue

        blockhash = proxy.getblockhash(i)
        block = proxy.getblock(blockhash)
        block_info = dict(difficulty=block.difficulty,
                          height=i,
                          time=block.nTime,
                          subsidy=0,
                          hashes_required=block.difficulty * 2**256 / (0xffff * 2**208))

        for tx in block.vtx:
            for i, txout in enumerate(tx.vout):
                out_dec = Decimal(txout.nValue) / 100000000
                if tx.is_coinbase():
                    block_info['subsidy'] += out_dec

        block_info['subsidy'] = str(block_info['subsidy'])

        block_cache["{}_{}".format(proxy.name, i)] = block_info
        blocks.append(block_info)

    return render_template("graph.html", blocks=blocks, start=start, step=step, proxy=proxy)

if __name__ == '__main__':
    app.run(debug=True)
