// Shared helpers for the "current LinkedIn" mock (2026 markup: generated ids, no legacy global-nav classes).
window.BASE = location.pathname.split('/').slice(0, 2).join('/');   // "/linkedin-new"
window.hasCookie = (n) => document.cookie.split('; ').some(c => c.startsWith(n + '=') && c.length > n.length + 1);
window.memberNav = () => '<header id="hdr_x9"><nav class="_n_k2" aria-label="Primary">' +
  ['feed/Home', 'mynetwork/My Network', 'jobs/Jobs', 'messaging/Messaging', 'notifications/Notifications']
    .map(x => { const [p, t] = x.split('/'); return '<a class="_l_' + p.length + '" href="' + BASE + '/' + p + '/">' + t + '</a>'; }).join('') +
  '<button class="_me_q1" aria-label="Me"><img alt="Photo of Asha Verma" width="24" height="24">Me</button></nav></header>';
window.guestNav = () => '<header><nav class="nav pt-1.5"><a href="' + BASE + '/">LinkedIn</a>' +
  '<a class="nav__button-tertiary" href="' + BASE + '/signup/">Join now</a><a class="nav__button-secondary" href="' + BASE + '/login/">Sign in</a></nav></header>';
