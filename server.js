/**
 * YACINEDEV — AI Worker (Railway)
 * ---------------------------------------------------------------
 * الهدف: تشغيل محادثة Kimi في مكان لا يقتله السيرفر المشترك عند
 * انقطاع اتصال الزائر. استضافة PHP فقط "تُسلّم" المهمة هنا وترجع
 * فوراً؛ هذا السيرفر يتحدث مع Kimi ويرسل التقدّم تدريجياً لاستضافة
 * PHP عبر api/job_update.php، والواجهة تستمر بالاستطلاع كالمعتاد.
 *
 * ⚠️ نطاق هذا الإصدار (المرحلة 1): محادثة نصية عادية فقط (تفكير ثم
 * رد نهائي)، بدون تنفيذ أدوات (طقس/QR/shell/workspace...). الأدوات
 * تحتاج بنية إضافية (وصول لملفات استضافتك) — تُضاف لاحقاً عند الحاجة.
 */
'use strict';

const express = require('express');
const crypto = require('crypto');

const PORT = process.env.PORT || 8080;
const WORKER_API_KEY = process.env.WORKER_API_KEY || '';
const KIMI_BASE_URL = (process.env.KIMI_BASE_URL || 'https://kimi.moonshot.cn').replace(/\/+$/, '');
const KIMI_USER_ID = process.env.KIMI_USER_ID || '';
let kimiAccess = process.env.KIMI_ACCESS_TOKEN || '';
let kimiRefresh = process.env.KIMI_REFRESH_TOKEN || '';

const CALLBACK_URL = process.env.CALLBACK_URL || ''; // مثال: https://موقعك/api/job_update.php
const CALLBACK_KEY = process.env.CALLBACK_KEY || '';

const app = express();
app.use(express.json({ limit: '2mb' }));

// ===== أدوات صغيرة =====
function log(...a) { console.log(new Date().toISOString(), ...a); }

function jwtValid(jwt) {
  if (!jwt) return false;
  const parts = jwt.split('.');
  if (parts.length < 2) return true;
  try {
    const pad = '='.repeat((4 - (parts[1].length % 4)) % 4);
    const b64 = (parts[1] + pad).replace(/-/g, '+').replace(/_/g, '/');
    const payload = JSON.parse(Buffer.from(b64, 'base64').toString('utf8'));
    if (!payload.exp) return true;
    return payload.exp > Math.floor(Date.now() / 1000) + 60;
  } catch (e) { return true; }
}

async function getAccessToken(force) {
  if (!force && jwtValid(kimiAccess)) return kimiAccess;
  if (!kimiRefresh) return kimiAccess;
  try {
    const res = await fetch(KIMI_BASE_URL + '/api/auth/token/refresh', {
      method: 'GET',
      headers: {
        'Authorization': 'Bearer ' + kimiRefresh,
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0',
        'X-Traffic-Id': KIMI_USER_ID,
      },
    });
    if (res.ok) {
      const d = await res.json();
      if (d && d.access_token) {
        kimiAccess = d.access_token;
        if (d.refresh_token) kimiRefresh = d.refresh_token;
      }
    }
  } catch (e) { log('refresh error', e.message); }
  return kimiAccess;
}

async function kimiHeaders() {
  const tok = await getAccessToken(false);
  return {
    'Authorization': 'Bearer ' + tok,
    'Content-Type': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'X-Traffic-Id': KIMI_USER_ID,
    'Accept': '*/*',
  };
}

async function createChat() {
  const headers = await kimiHeaders();
  const res = await fetch(KIMI_BASE_URL + '/api/chat', {
    method: 'POST',
    headers,
    body: JSON.stringify({ enter_method: 'new_chat', is_example: false, kimiplus_id: 'kimi', name: 'YACINEDEV' }),
  });
  const d = await res.json().catch(() => null);
  if (res.ok && d && d.id) return String(d.id);
  throw new Error('فشل إنشاء محادثة Kimi (HTTP ' + res.status + ')');
}

// ===== نداء الاستدعاء الخلفي لاستضافة PHP =====
async function callback(jobId, user, payload) {
  if (!CALLBACK_URL) return;
  try {
    await fetch(CALLBACK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Callback-Key': CALLBACK_KEY },
      body: JSON.stringify({ job_id: jobId, user, ...payload }),
    });
  } catch (e) { log('callback error', e.message); }
}

async function checkShouldStop(jobId, user) {
  if (!CALLBACK_URL) return false;
  try {
    const url = CALLBACK_URL + (CALLBACK_URL.includes('?') ? '&' : '?') +
      'status=1&job_id=' + encodeURIComponent(jobId) + '&user=' + encodeURIComponent(user);
    const res = await fetch(url, { headers: { 'X-Callback-Key': CALLBACK_KEY } });
    if (!res.ok) return false;
    const d = await res.json().catch(() => null);
    return !!(d && d.should_stop);
  } catch (e) { return false; }
}

// ===== حلقة البث الفعلية مع Kimi =====
const FOLLOWUP_PROMPT_SUFFIX =
  '\n\nبعد نهاية إجابتك مباشرة أضف بالضبط 3 أسئلة متابعة قصيرة بالعربية، مخصّصة لمحتوى الرد لا عامة، بهذا الشكل بالضبط:\n' +
  '@@FOLLOWUPS@@\nسؤال المتابعة 1\nسؤال المتابعة 2\nسؤال المتابعة 3\n@@';

async function runJob(job) {
  const { job_id: jobId, user, message, display_name: display } = job;
  let chatId = job.chat_id || '';
  let stopped = false;
  let abortCtrl = new AbortController();

  try {
    if (!chatId) {
      chatId = await createChat();
      await callback(jobId, user, { kimi_chat_id: chatId });
    }

    const sysPrompt =
      'أنت YACINEDEV Agent، تتحدث مع المستخدم ' + (display || user) + ' بالعربية.\n' +
      'اكتب Thought قصيراً (تحليل الطلب) ثم Final Answer: الرد الكامل بصيغة Markdown نظيفة (بدون HTML خام إطلاقاً).' +
      FOLLOWUP_PROMPT_SUFFIX;

    const headers = await kimiHeaders();
    headers['Referer'] = KIMI_BASE_URL + '/chat/' + chatId;
    const url = KIMI_BASE_URL + '/api/chat/' + encodeURIComponent(chatId) + '/completion/stream';

    let phase = 'think';
    let pending = '';
    let thinkingAcc = '';
    let answerAcc = '';
    let lastFlush = Date.now();
    let bufThink = '';
    let bufAnswer = '';
    let stopCheckAt = Date.now();

    const flush = async (force) => {
      const now = Date.now();
      if (!force && now - lastFlush < 400) return;
      lastFlush = now;
      if (bufThink || bufAnswer) {
        await callback(jobId, user, {
          thinking: bufThink || undefined,
          token: bufAnswer || undefined,
          step: 1,
        });
        bufThink = ''; bufAnswer = '';
      }
      // تحقّق دوري (كل ~5 ثوانٍ) هل طُلب إيقاف المهمة فعلياً (المستخدم غادر حقاً)
      if (now - stopCheckAt > 5000) {
        stopCheckAt = now;
        if (await checkShouldStop(jobId, user)) {
          stopped = true;
          abortCtrl.abort();
        }
      }
    };

    const onToken = async (piece) => {
      if (!piece) return;
      if (phase === 'answer') {
        answerAcc += piece;
        bufAnswer += piece;
        await flush(false);
        return;
      }
      pending += piece;
      const m = pending.match(/(?:Final\s*Answer|الإجابة\s*النهائية)\s*[:：]\s*/iu);
      if (m) {
        const idx = m.index;
        const before = pending.slice(0, idx);
        const after = pending.slice(idx + m[0].length);
        if (before) { thinkingAcc += before; bufThink += before; }
        phase = 'answer';
        pending = '';
        if (after) { answerAcc += after; bufAnswer += after; }
        await flush(true);
        return;
      }
      const keep = 24;
      if (pending.length > keep) {
        const emit = pending.slice(0, pending.length - keep);
        pending = pending.slice(pending.length - keep);
        thinkingAcc += emit; bufThink += emit;
        await flush(false);
      }
    };

    const res = await fetch(url, {
      method: 'POST',
      headers,
      signal: abortCtrl.signal,
      body: JSON.stringify({
        kimiplus_id: 'kimi',
        messages: [{ role: 'user', content: sysPrompt + '\n\nرسالة المستخدم:\n' + message }],
        refs: [], refs_file: [],
        use_math: false, use_research: false, use_search: false,
        extend: { sidebar: false, yd_agent: 'yacinedev_worker' },
      }),
    });

    if (!res.ok || !res.body) throw new Error('HTTP ' + res.status + ' من Kimi');

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let carry = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      carry += decoder.decode(value, { stream: true });
      const lines = carry.split('\n');
      carry = lines.pop() || '';
      for (const raw of lines) {
        const line = raw.trim();
        if (!line.startsWith('data: ')) continue;
        let d;
        try { d = JSON.parse(line.slice(6)); } catch (e) { continue; }
        if (d && d.event === 'cmpl' && typeof d.text === 'string') {
          await onToken(d.text);
        }
      }
    }
    if (phase === 'think' && pending) { thinkingAcc += pending; bufThink += pending; }
    await flush(true);

    if (stopped) {
      await callback(jobId, user, { status: 'stopped' });
      log('job stopped (user left)', jobId);
      return;
    }

    let finalAnswer = answerAcc.trim();
    if (!finalAnswer) finalAnswer = thinkingAcc.trim() || 'تم إكمال المهمة دون نص نهائي.';
    await callback(jobId, user, { answer_clean: finalAnswer, thinking_done: true, status: 'completed' });
    log('job completed', jobId);
  } catch (e) {
    if (!stopped) {
      log('job error', jobId, e.message);
      await callback(jobId, user, { status: 'failed', answer_clean: 'تعذّر إكمال المهمة: ' + e.message });
    }
  }
}

// ===== المسارات =====
app.get('/health', (req, res) => res.json({ ok: true }));

app.post('/run', (req, res) => {
  const key = req.get('X-Api-Key') || '';
  if (!WORKER_API_KEY || key !== WORKER_API_KEY) {
    return res.status(401).json({ ok: false, error: 'مفتاح API غير صحيح' });
  }
  const job = req.body || {};
  if (!job.job_id || !job.user || !job.message) {
    return res.status(422).json({ ok: false, error: 'job_id / user / message مطلوبة' });
  }
  // ردّ فوري — التنفيذ الفعلي يكمل بالخلفية بغض النظر عن هذا الطلب
  res.json({ ok: true, accepted: true, job_id: job.job_id });
  runJob(job).catch((e) => log('unhandled', e));
});

app.listen(PORT, () => log('YACINEDEV AI worker listening on', PORT));
