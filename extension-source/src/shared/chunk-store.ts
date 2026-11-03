/**
 * IndexedDB storage shared by every extension context (offscreen document, service worker, popup).
 *
 * - "chunks": each recorded audio piece, kept until the server has transcribed it,
 *   so a failed piece can be sent again later (Retry).
 * - "shots": screenshots taken in video mode, used for the Word report and MOM.md.
 */

import type { Track } from "./types.js";

const DB_NAME = "meetmind";
const DB_VERSION = 3;
const CHUNKS = "chunks";
const SHOTS = "shots";

export interface StoredChunk {
  key: string;
  sessionId: string;
  index: number;
  offset: number;
  track: Track;
  blob: Blob;
}

export interface StoredShot {
  key: string;
  sessionId: string;
  name: string;
  time: number;
  blob: Blob;
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = (event) => {
      const db = request.result;
      // Version 3 keys audio pieces by track too; older pieces cannot be retried, so start clean.
      if (event.oldVersion < 3 && db.objectStoreNames.contains(CHUNKS)) db.deleteObjectStore(CHUNKS);
      for (const name of [CHUNKS, SHOTS]) {
        if (!db.objectStoreNames.contains(name)) {
          db.createObjectStore(name, { keyPath: "key" }).createIndex("sessionId", "sessionId");
        }
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function run<T>(
  storeName: string,
  mode: IDBTransactionMode,
  action: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(storeName, mode);
      const request = action(tx.objectStore(storeName));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

/* ---------- audio pieces ---------- */

export function chunkKey(sessionId: string, index: number, track: Track): string {
  return `${sessionId}:${index}:${track}`;
}

export async function putChunk(chunk: Omit<StoredChunk, "key">): Promise<void> {
  await run(CHUNKS, "readwrite", (store) =>
    store.put({ ...chunk, key: chunkKey(chunk.sessionId, chunk.index, chunk.track) }),
  );
}

export async function getChunk(sessionId: string, index: number, track: Track): Promise<StoredChunk | undefined> {
  return run<StoredChunk | undefined>(CHUNKS, "readonly", (store) => store.get(chunkKey(sessionId, index, track)));
}

export async function deleteChunk(sessionId: string, index: number, track: Track): Promise<void> {
  await run(CHUNKS, "readwrite", (store) => store.delete(chunkKey(sessionId, index, track)));
}

/* ---------- screenshots ---------- */

export async function putShot(shot: Omit<StoredShot, "key">): Promise<void> {
  await run(SHOTS, "readwrite", (store) => store.put({ ...shot, key: `${shot.sessionId}:${shot.name}` }));
}

export async function getShot(sessionId: string, name: string): Promise<StoredShot | undefined> {
  return run<StoredShot | undefined>(SHOTS, "readonly", (store) => store.get(`${sessionId}:${name}`));
}

export async function listShots(sessionId: string): Promise<StoredShot[]> {
  const shots = await run<StoredShot[]>(SHOTS, "readonly", (store) =>
    store.index("sessionId").getAll(IDBKeyRange.only(sessionId)),
  );
  return shots.sort((a, b) => a.time - b.time);
}

/* ---------- cleanup ---------- */

/** Removes all stored audio and screenshots (new recording or reset). */
export async function clearAll(): Promise<void> {
  await run(CHUNKS, "readwrite", (store) => store.clear());
  await run(SHOTS, "readwrite", (store) => store.clear());
}
