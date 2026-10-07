// Mock of Naukri's post-apply screening chatbot.
(function () {
  const qs = window.CHAT_QUESTIONS || [];
  let i = 0;
  const drawer = document.querySelector('.chatbot_DrawerContentWrapper');
  const msgs = document.getElementById('msgs');
  const opts = document.getElementById('options');
  const box = document.querySelector('.textArea');
  function bot(text) { const li = document.createElement('li'); li.className = 'botItem'; li.innerHTML = '<div><span></span></div>'; li.querySelector('span').textContent = text; msgs.appendChild(li); }
  function user(text) { const li = document.createElement('li'); li.className = 'userItem'; li.textContent = text; msgs.appendChild(li); }
  function render() {
    opts.innerHTML = ''; box.textContent = '';
    const q = qs[i]; bot(q.text);
    if (q.options) { box.parentElement.style.display = 'none';
      q.options.forEach((o, k) => { const d = document.createElement('div'); d.className = 'ssrc__radio-btn-container';
        d.innerHTML = '<input type="radio" class="ssrc__radio" name="q' + i + '" id="o' + i + k + '" value="' + o + '"><label class="ssrc__label" for="o' + i + k + '">' + o + '</label>';
        opts.appendChild(d); });
    } else { box.parentElement.style.display = 'block'; }
  }
  function done() { drawer.style.display = 'none'; const m = document.createElement('span'); m.className = 'apply-message'; m.textContent = 'You have successfully applied to ' + document.querySelector('h1').textContent; document.body.appendChild(m);
    const b = document.getElementById('apply-button'); b.id = 'already-applied'; b.textContent = 'Applied'; b.disabled = true; }
  window.startApply = function () { if (!qs.length) return done(); drawer.style.display = 'block'; bot('Hi, thank you for showing interest. Please answer a few questions.'); render(); };
  document.querySelector('.sendMsg').addEventListener('click', function () {
    const q = qs[i]; let val = '';
    if (q.options) { const c = opts.querySelector('input:checked'); val = c ? c.value : ''; } else { val = box.textContent.trim(); }
    if (!val) return;
    window.ANSWERS = (window.ANSWERS || []).concat([val]); user(val); i++;
    if (i >= qs.length) setTimeout(done, 300); else setTimeout(render, 300);
  });
})();
