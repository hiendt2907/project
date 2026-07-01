"""Tài nguyên tĩnh của portal shell — PHỤC VỤ NGOÀI (external), KHÔNG inline.

CSP `default-src 'self'` chặn inline style/script (đúng). Nên CSS/JS ở đây được phục
vụ như file same-origin (`/assets/shell.css`, `/assets/app.js`) → nạp hợp lệ dưới CSP
nghiêm ngặt, không cần `unsafe-inline`/`unsafe-eval`. JS đọc cấu hình từ `data-*` trên
#root (không nội suy giá trị backend vào script → không cần nonce cho biến).
"""
from __future__ import annotations

SHELL_CSS = """
:root{--bg:#0b0f14;--surface:#11161d;--line:#1e2733;--text:#cdd9e5;--muted:#7d8ea3}
*{box-sizing:border-box}
body{font:14px/1.55 ui-monospace,SFMono-Regular,monospace;margin:0;background:var(--bg);color:var(--text)}
body[data-kind=provider]{--accent:#f0b429}
body[data-kind=tenant]{--accent:#6cb6ff}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;background:var(--surface);border-bottom:2px solid var(--accent)}
h1{font-size:15px;margin:0;color:var(--accent);letter-spacing:.02em}
main{max-width:880px;margin:0 auto;padding:28px 20px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
.v{font-size:15px;margin-bottom:14px;word-break:break-all}
.chip{display:inline-block;background:#0e1620;border:1px solid var(--line);border-radius:999px;padding:3px 11px;margin:0 6px 6px 0;color:var(--accent)}
button{font:inherit;cursor:pointer;border:1px solid var(--accent);background:transparent;color:var(--accent);border-radius:8px;padding:8px 16px}
button:hover{background:var(--accent);color:#0b0f14}
a.btn{display:inline-block;text-decoration:none;border:1px solid var(--accent);color:var(--accent);border-radius:8px;padding:9px 18px}
a.btn:hover{background:var(--accent);color:#0b0f14}
.state{padding:24px;text-align:center;color:var(--muted)}
.err{color:#ff7b72;border-color:#ff7b72}
.muted{color:var(--muted);font-size:12px}
.center{text-align:center}
.actions{margin-top:8px}
"""

SHELL_JS = """
'use strict';
(function () {
  var root = document.getElementById('root');
  var who = document.getElementById('whoami');
  var BASE = root.dataset.base, KIND = root.dataset.kind, LABEL = root.dataset.label;
  function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
  function login(){location.href='/auth/login'}
  window.__aoipLogin = login;
  function chips(a){return (a||[]).map(function(x){return '<span class=chip>'+esc(x)+'</span>'}).join('')||'<span class=muted>—</span>'}
  function btnLogin(label){var b=document.createElement('a');b.className='btn';b.href='#';b.textContent=label;
    b.addEventListener('click',function(e){e.preventDefault();login()});return b}
  function btnLogout(){var b=document.createElement('button');b.textContent='Đăng xuất';
    b.addEventListener('click',logout);return b}
  function logout(){
    fetch(BASE+'/logout',{method:'POST',credentials:'same-origin',headers:{'X-AOIP-CSRF':'1'}})
      .then(function(){render(null,'signed_out')});
  }
  function setState(html){var root=document.getElementById('root');root.innerHTML='';
    var wrap=document.createElement('div');wrap.innerHTML=html;while(wrap.firstChild)root.appendChild(wrap.firstChild)}
  function centerBtn(btn){var d=document.createElement('div');d.className='center';d.appendChild(btn);return d}
  function render(me,state){
    var root=document.getElementById('root');
    if(state==='signed_out'){who.textContent='';setState('<div class=card><div class=state>Đã đăng xuất. Phiên máy chủ đã bị thu hồi.</div></div>');
      root.querySelector('.card').appendChild(centerBtn(btnLogin('Đăng nhập lại')));return}
    if(state==='unauth'){who.textContent='';setState('<div class=card><div class="k">'+esc(LABEL)+'</div><div class=state>Bạn chưa đăng nhập. Xác thực qua nhà cung cấp OIDC để tiếp tục.</div></div>');
      root.querySelector('.card').appendChild(centerBtn(btnLogin('Đăng nhập bằng OIDC')));return}
    if(state==='forbidden'){setState('<div class="card err"><div class="k err">403 · Không có quyền</div><div class=state>Tài khoản của bạn không có vai trò hợp lệ cho portal này.</div></div>');
      root.querySelector('.card').appendChild(centerBtn(btnLogout()));return}
    if(state==='expired'){who.textContent='';setState('<div class="card err"><div class="k err">Phiên hết hạn</div><div class=state>Phiên máy chủ đã hết hạn hoặc bị thu hồi. Vui lòng đăng nhập lại.</div></div>');
      root.querySelector('.card').appendChild(centerBtn(btnLogin('Đăng nhập lại')));return}
    who.textContent=me.subject;
    var org='';
    if(KIND==='tenant'){
      var ms=me.memberships||{};
      org='<div class="k">Tổ chức đang hoạt động (server-side)</div><div class=v>'+esc(me.active_tenant||'—')+'</div>'
        +'<div class="k">Thành viên (không do client chọn)</div><div class=v>'
        +Object.keys(ms).map(function(t){return '<span class=chip>'+esc(t)+' · '+esc(ms[t])+'</span>'}).join('')+'</div>';
    }
    setState('<div class=card>'
      +'<div class="k">Danh tính</div><div class=v id=sub>'+esc(me.subject)+'</div>'
      +'<div class="k">Portal</div><div class=v>'+esc(LABEL)+' ('+esc(me.kind)+')</div>'
      +org
      +'<div class="k">Vai trò</div><div class=v id=roles>'+chips(me.roles)+'</div>'
      +'<div class="k">Quyền (backend-enforced)</div><div class=v id=perms>'+chips(me.permissions)+'</div>'
      +'<div id=actions class=actions></div></div>'
      +'<div class=muted>Quyền do backend cưỡng chế trên từng request; ẩn menu KHÔNG phải kiểm soát truy cập.</div>');
    document.getElementById('actions').appendChild(btnLogout());
  }
  window.__aoipRender = render;
  fetch(BASE+'/me',{credentials:'same-origin'}).then(function(r){
    if(r.status===401){return render(null,'unauth')}
    if(r.status===403){return render(null,'forbidden')}
    if(!r.ok){return render(null,'expired')}
    r.json().then(function(j){render(j,'ok')});
  }).catch(function(){render(null,'expired')});
})();
"""


def shell_html(title: str, ns: str, base: str, label: str) -> str:
    """HTML shell — CHỈ tham chiếu asset ngoài same-origin. Không inline style/script."""
    return (
        "<!doctype html><html lang=vi><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
        f"<title>{title}</title>"
        "<link rel=stylesheet href=\"/assets/shell.css\"></head>"
        f"<body data-kind=\"{ns}\">"
        f"<header><h1>{title}</h1><span id=whoami class=muted></span></header>"
        f"<main id=root data-base=\"{base}\" data-kind=\"{ns}\" data-label=\"{label}\">"
        "<div class=state>Đang tải phiên…</div></main>"
        "<script src=\"/assets/app.js\"></script></body></html>"
    )
