import { useMemo, useRef, useState } from 'react';
import { useApp } from '../context/appContext.js';
import { AREAS, CATEGORIES, TAGS } from '../data/options.js';
import { EXP_NOTE_MIN_CHARS } from '../utils/leveling.js';
import { PHOTO_MAX_DATA_URL_BYTES } from '../utils/storage.js';

// Stampport "오늘 다녀온 곳" form.
//
// The form deliberately makes "I was actually there" inputs first-class:
// - 방문 후기 (필수, ≥10자)
// - 오늘의 기분 (mood chip)
// - 사진 (선택, 자동 다운스케일)
// - 위치 인증 (선택, browser geolocation)
//
// Each input lights up a grade tier (C → S) and an EXP bonus row.
// Players see the preview update live as they fill the form, so the
// reward is visible *before* they commit. That's the RPG hook the
// brief asks for.

const MOODS = [
  { id: 'cozy',     label: '아늑함',         emoji: '☕' },
  { id: 'memorable', label: '기억에 남음',   emoji: '✨' },
  { id: 'with_someone', label: '누구랑 또', emoji: '🤝' },
  { id: 'alone',    label: '혼자 좋음',      emoji: '🌿' },
  { id: 'value',    label: '가성비 좋음',    emoji: '💰' },
  { id: 'dessert',  label: '디저트 천국',    emoji: '🍰' },
  { id: 'discovery', label: '새 발견',        emoji: '🧭' },
];

function todayInputValue() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

// Downscale an uploaded image to a square JPEG no larger than
// PHOTO_MAX_DATA_URL_BYTES. Returns a data URL or null on failure.
// Runs entirely in the browser — no upload, no network call.
async function downscaleImageFile(file, maxEdge = 720, quality = 0.78) {
  if (!file) return null;
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  return await new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const ratio = Math.min(1, maxEdge / Math.max(img.width, img.height));
      const w = Math.round(img.width * ratio);
      const h = Math.round(img.height * ratio);
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, w, h);
      // Try descending quality until we fit the budget.
      let q = quality;
      let out = canvas.toDataURL('image/jpeg', q);
      while (out.length > PHOTO_MAX_DATA_URL_BYTES && q > 0.4) {
        q -= 0.1;
        out = canvas.toDataURL('image/jpeg', q);
      }
      resolve(out);
    };
    img.onerror = reject;
    img.src = dataUrl;
  });
}

function GradePreview({ grade }) {
  return (
    <div className="grade-preview" data-grade={grade.grade}>
      <div className="gp-rank">
        <span className="rank">{grade.grade}</span>
        <span className="label">{grade.label}</span>
      </div>
      <ul className="gp-checks">
        {grade.checks.map((c) => (
          <li key={c.key} className={c.met ? 'met' : ''}>
            <span aria-hidden="true">{c.met ? '●' : '○'}</span>
            {c.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ExpPreview({ breakdown }) {
  return (
    <div className="exp-preview">
      <div className="ep-head">
        <span className="ep-label">예상 EXP</span>
        <span className="ep-total">+{breakdown.total}</span>
      </div>
      <ul>
        {breakdown.items.map((it) => (
          <li key={it.key}>
            <span>{it.label}</span>
            <strong>+{it.exp}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function StampForm({ navigate }) {
  const { addStamp, previewStamp } = useApp();
  const photoInputRef = useRef(null);

  const [placeName, setPlaceName] = useState('');
  const [area, setArea] = useState('성수');
  const [category, setCategory] = useState('cafe');
  const [tags, setTags] = useState([]);
  const [menu, setMenu] = useState('');
  const [visitedAt, setVisitedAt] = useState(todayInputValue());
  const [experienceNote, setExperienceNote] = useState('');
  const [visitMood, setVisitMood] = useState('');
  const [photoDataUrl, setPhotoDataUrl] = useState('');
  const [photoBusy, setPhotoBusy] = useState(false);
  const [locationLabel, setLocationLabel] = useState('');
  const [locationBusy, setLocationBusy] = useState(false);
  const [locationError, setLocationError] = useState('');
  const [error, setError] = useState('');

  const toggleTag = (tag) => {
    setTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag],
    );
  };

  const handlePhoto = async (event) => {
    const file = event.target?.files?.[0];
    if (!file) return;
    setPhotoBusy(true);
    try {
      const url = await downscaleImageFile(file);
      if (url) setPhotoDataUrl(url);
    } catch {
      // Silently swallow — the input is optional. The preview just
      // won't update.
    } finally {
      setPhotoBusy(false);
      if (photoInputRef.current) photoInputRef.current.value = '';
    }
  };

  const handleLocation = () => {
    if (locationBusy) return;
    setLocationError('');
    if (typeof navigator === 'undefined' || !navigator.geolocation) {
      setLocationError('이 기기에서는 위치 확인이 불가능해요.');
      return;
    }
    setLocationBusy(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords;
        const label =
          `${area} 일대 (GPS · ` +
          `${latitude.toFixed(3)}, ${longitude.toFixed(3)})`;
        setLocationLabel(label);
        setLocationBusy(false);
      },
      (err) => {
        setLocationBusy(false);
        setLocationError(
          err.code === 1
            ? '위치 권한이 거부됐어요. 브라우저 설정을 확인하세요.'
            : '위치를 확인하지 못했어요.'
        );
      },
      { timeout: 7000, maximumAge: 60_000 },
    );
  };

  const onSubmit = (event) => {
    event.preventDefault();
    if (!placeName.trim()) {
      setError('가게 이름을 입력해 주세요.');
      return;
    }
    if (experienceNote.trim().length < EXP_NOTE_MIN_CHARS) {
      setError(
        `방문 후기를 ${EXP_NOTE_MIN_CHARS}자 이상 적어 주세요. ` +
          '한 줄이면 충분해요 — 어떤 기억으로 남았나요?'
      );
      return;
    }
    const stamp = addStamp({
      place_name: placeName,
      area,
      category,
      tags,
      representative_menu: menu,
      visited_at: visitedAt,
      experience_note: experienceNote,
      photo_data_url: photoDataUrl,
      location_label: locationLabel,
      visit_mood: visitMood,
    });
    navigate(`/result/${stamp.id}`);
  };

  // Live preview of grade + EXP based on what's filled in right now.
  const preview = useMemo(
    () =>
      previewStamp({
        place_name: placeName,
        area,
        category,
        tags,
        experience_note: experienceNote,
        photo_data_url: photoDataUrl,
        location_label: locationLabel,
        visit_mood: visitMood,
      }),
    [
      previewStamp,
      placeName,
      area,
      category,
      tags,
      experienceNote,
      photoDataUrl,
      locationLabel,
      visitMood,
    ],
  );

  const noteChars = experienceNote.trim().length;
  const noteOk = noteChars >= EXP_NOTE_MIN_CHARS;

  return (
    <section className="form-stack" style={{ gap: 18 }}>
      <button type="button" className="back-btn" onClick={() => navigate('/passport')}>
        ← 여권으로
      </button>
      <div className="page-head">
        <span className="page-eyebrow">New Stamp</span>
        <h1>오늘의 도장 찍기</h1>
        <p>가게 이름만으로는 도장을 못 찍어요. 어떤 기억으로 남았는지 한 줄 적어주세요.</p>
      </div>

      {/* Live grade + EXP preview — updates as the form changes. */}
      <div className="stamp-preview-row">
        <GradePreview grade={preview.grade} />
        <ExpPreview breakdown={preview.breakdown} />
      </div>

      <form className="form-stack" onSubmit={onSubmit}>
        <div className="form-field">
          <label htmlFor="place_name">가게 이름</label>
          <input
            id="place_name"
            type="text"
            value={placeName}
            onChange={(e) => setPlaceName(e.target.value)}
            placeholder="예: 성수동 소금빵 베이커리"
            maxLength={40}
          />
        </div>

        <div className="form-field">
          <label>지역</label>
          <div className="tag-grid">
            {AREAS.map((a) => (
              <button
                key={a}
                type="button"
                className={`tag-chip ${area === a ? 'active' : ''}`}
                onClick={() => setArea(a)}
              >
                {a}
              </button>
            ))}
          </div>
        </div>

        <div className="form-field">
          <label>카테고리</label>
          <div className="choice-grid">
            {CATEGORIES.map((c) => (
              <button
                key={c.id}
                type="button"
                className={`choice ${category === c.id ? 'active' : ''}`}
                onClick={() => setCategory(c.id)}
              >
                <strong>
                  {c.icon} {c.label}
                </strong>
                <span>{categoryHint(c.id)}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="form-field">
          <label htmlFor="experience_note">
            방문 후기 <span className="form-required">필수</span>
          </label>
          <textarea
            id="experience_note"
            value={experienceNote}
            onChange={(e) => setExperienceNote(e.target.value)}
            placeholder="이 가게에서 가장 기억에 남는 한 가지를 적어주세요. 어떤 기분으로 나왔나요?"
            rows={3}
            maxLength={240}
          />
          <span
            className={`form-helper ${noteOk ? 'is-ok' : ''}`}
            style={{ color: noteOk ? 'var(--color-deep-green)' : 'var(--color-burgundy)' }}
          >
            {noteOk
              ? `좋아요 — ${noteChars}자 작성됨`
              : `${EXP_NOTE_MIN_CHARS}자 이상 작성하면 도장이 인증돼요. (현재 ${noteChars}자)`}
          </span>
        </div>

        <div className="form-field">
          <label>오늘의 기분</label>
          <div className="mood-grid">
            {MOODS.map((m) => (
              <button
                key={m.id}
                type="button"
                className={`mood-chip ${visitMood === m.id ? 'active' : ''}`}
                onClick={() =>
                  setVisitMood((prev) => (prev === m.id ? '' : m.id))
                }
              >
                <span className="mood-emoji" aria-hidden="true">
                  {m.emoji}
                </span>
                <span className="mood-label">{m.label}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="form-field">
          <label>방문 인증</label>
          <div className="proof-grid">
            <div className="proof-tile">
              <div className="proof-head">
                <span className="proof-icon" aria-hidden="true">📸</span>
                <span>사진</span>
              </div>
              {photoDataUrl ? (
                <>
                  <img
                    src={photoDataUrl}
                    alt="첨부된 사진 미리보기"
                    className="proof-photo"
                  />
                  <div className="proof-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-mini"
                      onClick={() => setPhotoDataUrl('')}
                    >
                      제거
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <p className="proof-sub">
                    그날 분위기를 한 장 첨부하면 도장 등급이 올라가요.
                  </p>
                  <label className="btn btn-secondary btn-mini">
                    {photoBusy ? '처리 중…' : '사진 선택'}
                    <input
                      ref={photoInputRef}
                      type="file"
                      accept="image/*"
                      onChange={handlePhoto}
                      hidden
                    />
                  </label>
                </>
              )}
            </div>

            <div className="proof-tile">
              <div className="proof-head">
                <span className="proof-icon" aria-hidden="true">📍</span>
                <span>위치</span>
              </div>
              {locationLabel ? (
                <>
                  <p className="proof-sub proof-loc">{locationLabel}</p>
                  <div className="proof-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-mini"
                      onClick={() => setLocationLabel('')}
                    >
                      해제
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <p className="proof-sub">
                    현재 위치를 확인하면 발급일/도시 도장이 또렷해져요.
                  </p>
                  <button
                    type="button"
                    className="btn btn-secondary btn-mini"
                    onClick={handleLocation}
                    disabled={locationBusy}
                  >
                    {locationBusy ? '확인 중…' : '현재 위치 확인'}
                  </button>
                  {locationError && (
                    <span
                      className="form-helper"
                      style={{ color: 'var(--color-burgundy)' }}
                    >
                      {locationError}
                    </span>
                  )}
                </>
              )}
            </div>
          </div>
        </div>

        <div className="form-field">
          <label>태그 (최대 5개)</label>
          <div className="tag-grid">
            {TAGS.map((tag) => {
              const active = tags.includes(tag);
              const disabled = !active && tags.length >= 5;
              return (
                <button
                  key={tag}
                  type="button"
                  className={`tag-chip ${active ? 'active' : ''}`}
                  onClick={() => !disabled && toggleTag(tag)}
                  style={disabled ? { opacity: 0.4 } : undefined}
                >
                  {tag}
                </button>
              );
            })}
          </div>
        </div>

        <div className="form-field">
          <label htmlFor="menu">대표 메뉴</label>
          <input
            id="menu"
            type="text"
            value={menu}
            onChange={(e) => setMenu(e.target.value)}
            placeholder="예: 소금빵, 라떼, 크림 파스타"
            maxLength={40}
          />
        </div>

        <div className="form-field">
          <label htmlFor="visited_at">방문일</label>
          <input
            id="visited_at"
            type="date"
            value={visitedAt}
            onChange={(e) => setVisitedAt(e.target.value)}
            max={todayInputValue()}
          />
        </div>

        {error ? (
          <p className="form-helper" style={{ color: 'var(--color-burgundy)' }}>
            {error}
          </p>
        ) : null}

        <button type="submit" className="btn btn-primary btn-block">
          스탬프 찍기 · {preview.grade.grade}등급 · +{preview.breakdown.total} EXP
        </button>
      </form>
    </section>
  );
}

function categoryHint(id) {
  switch (id) {
    case 'cafe':
      return '커피와 분위기';
    case 'bakery':
      return '빵과 디저트';
    case 'restaurant':
      return '한 끼와 코스';
    case 'dessert':
      return '단 한 입의 행복';
    default:
      return '';
  }
}
