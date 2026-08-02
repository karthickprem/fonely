import 'dotenv/config';
import { createServer } from 'http';
import { readFileSync, existsSync } from 'fs';
import { join, extname } from 'path';
import { fileURLToPath } from 'url';
import { WebSocketServer } from 'ws';
import { handleExotelWebSocket, getActiveCalls } from './call-handler.js';
import { handleBrowserWebSocket } from './browser-handler.js';
import { handleVoiceLabSession } from './voice/session.js';

const PORT = process.env.PORT || 3000;
const __dirname = fileURLToPath(new URL('.', import.meta.url));
const PUBLIC_DIR = join(__dirname, '..', 'public');

const MIME_TYPES = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
};

function serveStatic(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  let filePath;

  if (url.pathname === '/voice-lab') {
    filePath = join(PUBLIC_DIR, 'voice-lab.html');
  } else if (url.pathname.startsWith('/public/') || url.pathname.endsWith('.js') || url.pathname.endsWith('.css')) {
    filePath = join(PUBLIC_DIR, url.pathname.replace(/^\/public\//, ''));
  } else {
    return false;
  }

  if (!existsSync(filePath)) return false;

  const ext = extname(filePath);
  const mime = MIME_TYPES[ext] || 'application/octet-stream';
  const content = readFileSync(filePath);
  res.writeHead(200, { 'Content-Type': mime });
  res.end(content);
  return true;
}

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

  if (serveStatic(req, res)) return;

  res.writeHead(404);
  res.end('Not found');
});

const wssVoice = new WebSocketServer({ noServer: true });
wssVoice.on('connection', (ws) => handleExotelWebSocket(ws));

const wssBrowser = new WebSocketServer({ noServer: true });
wssBrowser.on('connection', (ws) => handleBrowserWebSocket(ws));

const wssVoiceLab = new WebSocketServer({ noServer: true });
wssVoiceLab.on('connection', (ws) => handleVoiceLabSession(ws));

httpServer.on('upgrade', (req, socket, head) => {
  if (req.url === '/voice') {
    wssVoice.handleUpgrade(req, socket, head, (ws) => wssVoice.emit('connection', ws, req));
  } else if (req.url === '/demo-ws') {
    wssBrowser.handleUpgrade(req, socket, head, (ws) => wssBrowser.emit('connection', ws, req));
  } else if (req.url === '/voice-lab-ws') {
    wssVoiceLab.handleUpgrade(req, socket, head, (ws) => wssVoiceLab.emit('connection', ws, req));
  } else {
    socket.destroy();
  }
});

httpServer.listen(PORT, '0.0.0.0', () => {
  console.log(`
  ╔═══════════════════════════════════════════╗
  ║     Fonely Voice R&D Lab v0.2.0          ║
  ║                                           ║
  ║  Voice Lab:  http://localhost:${PORT}/voice-lab  ║
  ║  Demo:       http://localhost:${PORT}/demo       ║
  ║  Health:     http://localhost:${PORT}/health      ║
  ╚═══════════════════════════════════════════╝
  `);
});
