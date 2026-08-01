import 'dotenv/config';
import { createServer } from 'http';
import { readFileSync } from 'fs';
import { WebSocketServer } from 'ws';
import { handleExotelWebSocket, getActiveCalls } from './call-handler.js';
import { handleBrowserWebSocket } from './browser-handler.js';

const PORT = process.env.PORT || 3000;

const httpServer = createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', activeCalls: getActiveCalls() }));
    return;
  }

  if (req.url === '/' || req.url === '/demo') {
    const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf-8');
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(html);
    return;
  }

  res.writeHead(404);
  res.end('Not found');
});

// Exotel AgentStream WebSocket (for real phone calls)
const wssVoice = new WebSocketServer({ noServer: true });
wssVoice.on('connection', (ws) => handleExotelWebSocket(ws));

// Browser demo WebSocket (for testing from phone/laptop browser)
const wssBrowser = new WebSocketServer({ noServer: true });
wssBrowser.on('connection', (ws) => handleBrowserWebSocket(ws));

httpServer.on('upgrade', (req, socket, head) => {
  if (req.url === '/voice') {
    wssVoice.handleUpgrade(req, socket, head, (ws) => wssVoice.emit('connection', ws, req));
  } else if (req.url === '/demo-ws') {
    wssBrowser.handleUpgrade(req, socket, head, (ws) => wssBrowser.emit('connection', ws, req));
  } else {
    socket.destroy();
  }
});

httpServer.listen(PORT, '0.0.0.0', () => {
  console.log(`
  ╔═══════════════════════════════════════════╗
  ║       Fonely Voice Server v0.1.0          ║
  ║                                           ║
  ║  Demo:    http://localhost:${PORT}/demo        ║
  ║  Exotel:  ws://localhost:${PORT}/voice         ║
  ║  Health:  http://localhost:${PORT}/health       ║
  ╚═══════════════════════════════════════════╝
  `);
});
