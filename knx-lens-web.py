from textual_serve.server import Server

server = Server("python -m knx-lens")
server.serve()