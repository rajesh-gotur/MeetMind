/**
 * IndexedDB storage shared by every extension context (offscreen document, service worker, popup).
 *
 * - "chunks": each recorded audio piece, kept until the server has transcribed it,
 *   so a failed piece can be sent again later (Retry).
 * - "shots": screenshots taken in video mode, used for the Word report and MOM.md.
 */
const DB_NAME = "meetmind";
const DB_VERSION = 3;
const CHUNKS = "chunks";
const SHOTS = "shots";
function open() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = (event) => {
            const db = request.result;
            // Version 3 keys audio pieces by track too; older pieces cannot be retried, so start clean.
            if (event.oldVersion < 3 && db.objectStoreNames.contains(CHUNKS))
                db.deleteObjectStore(CHUNKS);
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
async function run(storeName, mode, action) {
    const db = await open();
    try {
        return await new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, mode);
            const request = action(tx.objectStore(storeName));
            tx.oncomplete = () => resolve(request.result);
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error);
        });
    }
    finally {
        db.close();
    }
}
/* ---------- audio pieces ---------- */
export function chunkKey(sessionId, index, track) {
    return `${sessionId}:${index}:${track}`;
}
export async function putChunk(chunk) {
    await run(CHUNKS, "readwrite", (store) => store.put({ ...chunk, key: chunkKey(chunk.sessionId, chunk.index, chunk.track) }));
}
export async function getChunk(sessionId, index, track) {
    return run(CHUNKS, "readonly", (store) => store.get(chunkKey(sessionId, index, track)));
}
export async function deleteChunk(sessionId, index, track) {
    await run(CHUNKS, "readwrite", (store) => store.delete(chunkKey(sessionId, index, track)));
}
/* ---------- screenshots ---------- */
export async function putShot(shot) {
    await run(SHOTS, "readwrite", (store) => store.put({ ...shot, key: `${shot.sessionId}:${shot.name}` }));
}
export async function getShot(sessionId, name) {
    return run(SHOTS, "readonly", (store) => store.get(`${sessionId}:${name}`));
}
export async function listShots(sessionId) {
    const shots = await run(SHOTS, "readonly", (store) => store.index("sessionId").getAll(IDBKeyRange.only(sessionId)));
    return shots.sort((a, b) => a.time - b.time);
}
/* ---------- cleanup ---------- */
/** Removes all stored audio and screenshots (new recording or reset). */
export async function clearAll() {
    await run(CHUNKS, "readwrite", (store) => store.clear());
    await run(SHOTS, "readwrite", (store) => store.clear());
}
