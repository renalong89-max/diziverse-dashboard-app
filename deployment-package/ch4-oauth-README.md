# ch4 YouTube OAuth Setup — User Steps

ch4 ka OAuth token purane server ke sath zaya ho gaya. Naya token banane ke liye
**aapko khud** Google par "Allow" click karna parega (agent ye click nahi kar sakta).

## Pehle se taiyar

- Google Cloud Project **"DiziVerse Uploader 10"** pehle se bana hua hai
  (2026-10-07 ko banaya tha), YouTube Data API v3 enabled hai.
- Agar wo project ab bhi hai to naya project banane ki zaroorat nahi —
  usi project ka OAuth client use hoga.

## Steps (aapke liye)

1. **Agent aapko ek link dega** (Google OAuth consent URL).
   Ye link ch4 ke YouTube channel ke liye hoga.

2. **Link kholo** — apne us Google account se login karo jo
   ch4 YouTube channel ("Dizi Dünyası Deniz") ka owner hai.

3. **"Allow" par click karo** — YouTube Data API ko permission do.
   - ⚠️ Password **kabhi** agent ko mat dena, aur kabhi change mat karna.
   - Sirf **Allow** ka button aapko dabana hai.

4. **Code copy karo** — Google ek authorization code dikhayega.
   Wo code agent ko bhej do (main chat mein, ya WhatsApp par).

5. **Agent token banayega** — code se `token.json` generate hoga aur
   `~/diziverse-server/ch4/token.json` mein save hoga (mode 0600).

## Verify

Agent ye check karega:
- `token.json` valid JSON hai
- YouTube API `channels.list(mine=true)` sahi channel ka naam deta hai
- Dashboard par ch4 "connected" dikhe

## Note

- ch2 aur ch3 ke tokens backup se restore ho jayenge — unke liye
  aapko kuch nahi karna.
- Sirf ch4 ke liye ye ek dafa ka step hai.
