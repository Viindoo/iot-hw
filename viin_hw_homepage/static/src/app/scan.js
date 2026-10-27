/* Looking for the Wi-Fi networks the radio hears. This file imports nothing, so that a test loads it alone. */

// A radio that has just served the access point, or has just come back from a join that failed, may hear nothing at
// first: the scan is asked again this many times in all, this long apart (milliseconds).
export const SCAN_TRIES = 4;
export const SCAN_PAUSE = 3000;

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

/* The names `scan` gives, asked again while it gives none; an empty list only when every try heard nothing. */
export async function scanUntilHeard(scan, { tries = SCAN_TRIES, pause = SCAN_PAUSE, wait = sleep, stopped = () => false } = {}) {
    let names = [];
    for (let attempt = 1; attempt <= tries && !stopped(); attempt++) {
        names = await scan();
        if (names.length || attempt === tries) {
            break;
        }
        await wait(pause);
    }
    return names;
}
