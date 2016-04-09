import requests
import json
from decimal import Decimal
from bitcoin.rpc import Proxy
from requests.auth import HTTPBasicAuth
from flask import Flask, jsonify, render_template

app = Flask(__name__)
access = Proxy("http://bitmark:dG0s0t7VgwawUqqzksMC@localhost:9266")
block_cache = {}


@app.route('/<start>/<end>/')
def hello_world(start, end):
    end = int(end)
    start = int(start)
    blocks = []
    for i in range(start, end):
        if i in block_cache:
            blocks.append(block_cache[i])
            continue

        blockhash = access.getblockhash(i)
        block = access.getblock(blockhash)
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

        block_cache[i] = block_info
        blocks.append(block_info)

    return render_template("index.html", blocks=blocks)

if __name__ == '__main__':
    app.run(debug=True)
