from flask import Flask

app = Flask(__name__)
import routes.showdown
import routes.ghost_chains
