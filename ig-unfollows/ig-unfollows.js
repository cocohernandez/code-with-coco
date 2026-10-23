// ─────────────────────────────────────────────────────────────
//   ig-unfollows
//   finding out who unfollowed you and didn't remove you from
//   their following
// ─────────────────────────────────────────────────────────────
//
//   what this is:
//   a tiny script that runs in your browser console on
//   instagram.com to compare your followers against your
//   following — surfacing the accounts you follow that don't
//   follow you back. no app installs, no third-party logins,
//   no shady "track your unfollowers" services. just you, the
//   browser, and a little bit of javascript.
//
//   heads up:
//   you must be signed into instagram in the same browser tab
//   for this to work — the script pulls your data through
//   instagram's own internal API, which only responds to logged-
//   in sessions.
//
//   you might also see warnings or errors pop up in the console
//   while it runs. that's just instagram complaining about the
//   requests — totally fine, the script will keep going and
//   spit out your results when it's done.
//
// ─────────────────────────────────────────────────────────────
//   credit:
//   inspired by @abir-taheer's gist on github
// ─────────────────────────────────────────────────────────────
//   made by coco hernandez ♡
//
//   tiktok      @cocopuffffffffs
//   instagram   @cocohdzz
//   github      github.com/cocohernandez
//
//   part of my series "code with coco"! random little projects i build
//   for fun. you don't have to be a CS person to follow along.
// ─────────────────────────────────────────────────────────────

if (window.location.origin !== "https://www.instagram.com") {
  alert("hop over to instagram.com first, then paste this again.");
  window.location.href = "https://www.instagram.com";
}

const PAGE_SIZE = 200;       // big pages → fewer requests
const SLEEP_MIN = 400;       // gentle on rate limits
const SLEEP_MAX = 900;

const fetchOptions = {
  credentials: "include",
  headers: { "X-IG-App-ID": "936619743392459" },
  method: "GET",
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitter = (min, max) => Math.floor(Math.random() * (max - min)) + min;

const fetchAllPages = async (list, userId) => {
  const users = [];
  let nextMaxId = "";
  while (true) {
    const params = new URLSearchParams({ count: PAGE_SIZE });
    if (nextMaxId) params.set("max_id", nextMaxId);
    const url = `https://www.instagram.com/api/v1/friendships/${userId}/${list}/?${params}`;
    const res = await fetch(url, fetchOptions);
    if (!res.ok) throw new Error(`${list} request failed: ${res.status}`);
    const data = await res.json();
    users.push(...data.users);
    if (!data.next_max_id) return users;
    nextMaxId = data.next_max_id;
    await sleep(jitter(SLEEP_MIN, SLEEP_MAX));
  }
};

// reads your own user ID from the login cookie instead of using the search endpoint
const getUserId = async () => document.cookie.match(/ds_user_id=(\d+)/)?.[1];

const findUnfollowers = async (username) => {
  const userId = await getUserId();
  if (!userId) throw new Error("couldn't find your user ID — make sure you're logged in on instagram.com");

  console.log(`%cfetching followers + following for @${username} in parallel...`, "color:#ff7ad9;font-weight:bold");
  const t0 = performance.now();

  // both lists fetched concurrently → ~half the wall time
  const [followers, following] = await Promise.all([
    fetchAllPages("followers", userId),
    fetchAllPages("following", userId),
  ]);

  const followerSet = new Set(followers.map((u) => u.username.toLowerCase()));
  const dontFollowBack = following
    .filter((u) => !followerSet.has(u.username.toLowerCase()))
    .map((u) => u.username)
    .sort();

  const secs = ((performance.now() - t0) / 1000).toFixed(1);
  console.log(`%c↓ doesn't follow you back (${dontFollowBack.length}/${following.length}) — ${secs}s`, "color:#9d6cff;font-weight:bold;font-size:14px");
  dontFollowBack.forEach((u) => console.log(`  · ${u}`));
  return dontFollowBack;
};

// TODO: change this to your username
const yourUsername = "placeholder";     // keep the quotes around it
findUnfollowers(yourUsername);