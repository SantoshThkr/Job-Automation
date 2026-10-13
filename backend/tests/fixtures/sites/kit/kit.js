// The same application questions on every portal mock: a regression kit for the central answer/form engine.
// kitRender(container, keys, {company, phoneCheck: 'pattern' | 'submit'}) renders them; kitValid() lists the
// site's validation messages; kitData() returns what would be sent.
(function () {
  const sel = (id, label, opts, req = true) => `<div class="kq"><label for="${id}">${label}</label><select id="${id}"${req ? ' required' : ''}>` +
    opts.map((o, i) => `<option${i === 0 ? ' value=""' : ''}>${o}</option>`).join('') + '</select></div>';
  const text = (id, label, extra = '') => `<div class="kq"><label for="${id}">${label}</label><input id="${id}" type="text" required ${extra}></div>`;
  const Q = {
    // known profile facts
    email: () => `<div class="kq"><label for="k_email">Email address*</label><input id="k_email" type="email" required></div>`,
    phone: (o) => `<div class="kq"><label for="k_phone">Phone*</label><input id="k_phone" type="text" required` +
      (o.phoneCheck === 'pattern' ? ' pattern="[0-9]{10}"' : '') + '></div>' +
      // a widget-internal checkbox with no label of its own, right after the phone box (LinkedIn 2026)
      '<input type="checkbox" class="k_deco" tabindex="-1">',
    title: () => text('k_title', 'Job Title*'),
    github: () => '<div class="kq"><div class="q">GitHub profile*</div><input id="k_gh" type="text" required ' +
      'placeholder="Enter URL here" aria-label="Enter URL here"></div>',
    // derived facts
    months: () => sel('k_months', 'Please select your total additional months of experience*',
      ['Select an option', ...Array.from({ length: 12 }, (_, i) => String(i))]),
    located: (o) => sel('k_loc', 'Are you located in Bengaluru?*', ['Select an option', 'Yes', 'No']),
    fresher: () => '<fieldset class="kq" id="k_fresher"><legend>Are you a fresher?*</legend>' +
      '<div class="card-opt"><input type="radio" name="k_fresher" value="on" required><span>Yes</span></div>' +
      '<div class="card-opt"><input type="radio" name="k_fresher" value="on"><span>No</span></div></fieldset>',
    authorized: () => '<fieldset class="kq"><legend>Are you legally authorized to work in India?*</legend>' +
      '<input type="radio" id="k_au_y" name="k_au" value="Yes" required><label for="k_au_y">Yes</label>' +
      '<input type="radio" id="k_au_n" name="k_au" value="No"><label for="k_au_n">No</label></fieldset>',
    // only you can answer (no citizenship in the profile)
    workauth: () => sel('k_wa', 'Work Authorization*', ['Select an option', 'Citizen', 'Permanent Resident', 'Work Visa']),
    blood: () => text('k_blood', 'What is your blood group?*'),
    // reused from your saved answer (asked once before with other words)
    graphql: () => text('k_gql', 'How many years of work experience do you have with GraphQL?*'),
    // custom control: a button + listbox, no <select>
    notice: () => '<div class="kq"><span id="k_np_l">Notice period*</span>' +
      '<button type="button" id="k_np" aria-haspopup="listbox" aria-labelledby="k_np_l">Select…</button>' +
      '<ul role="listbox" id="k_np_list" hidden>' + ['Immediate', '15 days', '30 days', '60 days', '90 days']
        .map(x => `<li role="option">${x}</li>`).join('') + '</ul><input type="hidden" id="k_np_v" required></div>',
    // written from your verified profile + the job
    why: (o) => `<div class="kq"><label for="k_why">Why do you want to join ${o.company}?*</label>` +
      '<textarea id="k_why" required maxlength="1000"></textarea></div>',
    // one entry of a work-experience list (prefilled by the site from your profile there)
    exp: () => '<div class="kq-exp"><h4>Work experience</h4>' +
      '<label for="k_t1">Title*</label><input id="k_t1" type="text" value="Frontend Developer" required>' +
      '<label for="k_c1">Company*</label><input id="k_c1" type="text" value="Pixel Labs" required>' +
      '<label for="k_d1">Description*</label><textarea id="k_d1" required maxlength="2000"></textarea></div>',
  };
  window.kitRender = function (container, keys, opts) {
    opts = Object.assign({ company: 'KitCo', phoneCheck: 'submit' }, opts || {});
    container.insertAdjacentHTML('beforeend', keys.map(k => Q[k](opts)).join(''));
    const btn = container.querySelector('#k_np');
    if (btn) {
      const list = container.querySelector('#k_np_list');
      btn.onclick = () => { list.hidden = !list.hidden; };
      list.querySelectorAll('[role=option]').forEach(li => li.onclick = () => {
        btn.textContent = li.textContent; container.querySelector('#k_np_v').value = li.textContent; list.hidden = true; });
      document.addEventListener('keydown', e => { if (e.key === 'Escape') list.hidden = true; });
    }
  };
  window.kitValid = function (container) {
    const errs = [];
    container.querySelectorAll('[required]').forEach(el => {
      if (el.type === 'radio') { if (!container.querySelector(`input[name="${el.name}"]:checked`)) errs.push('Please answer: ' + el.name); }
      else if (!String(el.value || '').trim()) errs.push('This field is required: ' + (el.id || el.name));
      else if (el.checkValidity && !el.checkValidity()) errs.push('Invalid value: ' + el.id);
    });
    const ph = container.querySelector('#k_phone');
    if (ph && ph.value && !/^\d{10}$/.test(ph.value)) errs.push('Please enter a valid phone number.');
    return errs;
  };
  window.kitData = function (container) {
    const out = {};
    container.querySelectorAll('input, select, textarea').forEach(el => {
      if (el.type === 'radio') { if (el.checked) out[el.name] = el.value === 'on' ? el.parentElement.innerText.trim() : el.value; }
      else if (el.type === 'checkbox') { out[el.id || el.className] = el.checked; }
      else if (el.type !== 'file' && el.id) out[el.id] = el.value;
    });
    return out;
  };
})();
