import logging
import socket

from routes import app

logger = logging.getLogger(__name__)


@app.route('/', methods=['GET'])
def default_route():
    return 'Python Template'


logger = logging.getLogger()
handler = logging.StreamHandler()
formatter = logging.Formatter(
    '%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

if __name__ == "__main__":
    logging.info("Starting application ...")
    app.run(port=8080, debug=True)
