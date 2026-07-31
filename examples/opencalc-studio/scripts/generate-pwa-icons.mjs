import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const outputDirectory = resolve(scriptDirectory, '../public/icons');

const colors = {
  dark: [17, 21, 18, 255],
  green: [101, 197, 143, 255],
  light: [242, 244, 239, 255],
};

function crc32(buffer) {
  let crc = 0xffffffff;

  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }

  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const typeBuffer = Buffer.from(type);
  const body = Buffer.concat([typeBuffer, data]);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(body));

  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  return Buffer.concat([length, body, checksum]);
}

function isInsideRoundedSquare(x, y, left, top, size, radius) {
  const right = left + size;
  const bottom = top + size;
  const nearestX = Math.max(left + radius, Math.min(x, right - radius));
  const nearestY = Math.max(top + radius, Math.min(y, bottom - radius));
  const dx = x - nearestX;
  const dy = y - nearestY;
  return (
    (x >= left + radius && x <= right - radius && y >= top && y <= bottom) ||
    (y >= top + radius && y <= bottom - radius && x >= left && x <= right) ||
    dx * dx + dy * dy <= radius * radius
  );
}

function createIcon(size, maskable = false) {
  const scanlines = Buffer.alloc((size * 4 + 1) * size);
  const glyphMargin = size * (maskable ? 0.2 : 0.1);
  const glyphSize = size - glyphMargin * 2;
  const glyphRadius = size * 0.16;
  const dotRadius = size * (maskable ? 0.035 : 0.045);
  const dotOffset = glyphSize * 0.18;
  const center = size / 2;

  for (let y = 0; y < size; y += 1) {
    const rowStart = y * (size * 4 + 1);
    scanlines[rowStart] = 0;

    for (let x = 0; x < size; x += 1) {
      let color = colors.dark;
      if (
        isInsideRoundedSquare(
          x + 0.5,
          y + 0.5,
          glyphMargin,
          glyphMargin,
          glyphSize,
          glyphRadius,
        )
      ) {
        color = colors.green;
      }

      for (const dotX of [center - dotOffset, center + dotOffset]) {
        for (const dotY of [center - dotOffset, center + dotOffset]) {
          const dx = x + 0.5 - dotX;
          const dy = y + 0.5 - dotY;
          if (dx * dx + dy * dy <= dotRadius * dotRadius) {
            color = colors.light;
          }
        }
      }

      const pixelStart = rowStart + 1 + x * 4;
      scanlines.set(color, pixelStart);
    }
  }

  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8;
  header[9] = 6;

  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(scanlines, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

mkdirSync(outputDirectory, { recursive: true });
writeFileSync(resolve(outputDirectory, 'opencalc-192.png'), createIcon(192));
writeFileSync(resolve(outputDirectory, 'opencalc-512.png'), createIcon(512));
writeFileSync(
  resolve(outputDirectory, 'opencalc-maskable-512.png'),
  createIcon(512, true),
);
