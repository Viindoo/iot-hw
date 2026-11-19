/* Looking for the Wi-Fi networks the radio hears. This file imports nothing, so that a test loads it alone. */

// A radio that has just served the access point, or has just come back from a join that failed, may hear nothing at
// first, and the box itself is away while it changes network: the scan is asked again, this long apart, for at most
// this long in all (milliseconds).
export const SCAN_PAUSE = 3000;
export const SCAN_LIMIT = 90000;

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

/* Looks for the networks until one is heard or the limit has passed. `scan` answers the names the box gave, or null
   when the box did not answer. Gives {names, heardNothing}: `heardNothing` is true when at least one scan was answered
   and every answer was an empty list; both are empty and false when the box never answered. */
export async function scanUntilHeard(
    scan,
    {
        limit = SCAN_LIMIT,
        pause = SCAN_PAUSE,
        wait = sleep,
        now = Date.now,
        stopped = () => false,
    } = {}
) {
    const start = now();
    let answered = false;
    while (!stopped()) {
        const names = await scan();
        if (Array.isArray(names)) {
            answered = true;
            if (names.length) {
                return { names, heardNothing: false };
            }
        }
        if (stopped() || now() - start + pause >= limit) {
            break;
        }
        await wait(pause);
    }
    return { names: [], heardNothing: answered };
}

/* The rows of the list under the name field: the names heard, without an empty name and without a repeat, that hold
   the typed text (upper or lower case alike); the whole list while nothing is typed. */
export function listedNetworks(heard, typed) {
    const text = String(typed || "")
        .trim()
        .toLowerCase();
    const rows = [];
    for (const name of heard || []) {
        if (typeof name !== "string" || !name.trim() || rows.includes(name)) {
            continue;
        }
        if (!text || name.toLowerCase().includes(text)) {
            rows.push(name);
        }
    }
    return rows;
}
