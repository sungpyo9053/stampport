import { useMemo, useRef, useState } from 'react';
import { useApp } from '../context/appContext.js';
import {
  CATEGORIES,
  SUGGESTED_AREAS,
  TAGS,
  VISIT_PURPOSES,
  visitPurposeLabel,
} from '../data/options.js';
import {
  EXP_NOTE_MIN_CHARS,
  nextVerificationHint,
  verificationDef,
} from '../utils/leveling.js';
import { PHOTO_MAX_DATA_URL_BYTES } from '../utils/storage.js';

// "오늘 다녀온 곳" form — the moment the visit gets sealed into the
// passport. Inputs are organized so the player commits to *evidence*
// before the button activates: place + area + category + (menu OR
// purpose) + 한줄 후기 + 태그 1개. On top of that, photo and location
// upgrade the verification ladder (manual → location → photo →
// verified) which decides EXP and grade.

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

function VerificationPreview({ level, hint, totalExp }) {
  const def = verificationDef(level);
  return (
    <div className="verification-preview" data-grade={def.grade}>
      <div className="vp-head">
        <span className={`vp-tier vp-tier-${def.grade}`}>
          <strong>{def.grade}</strong>
          <em>등급</em>
        </span>
        <div className="vp-text">
          <div className="vp-name">{def.label}</div>
          <div className="vp-sub">{def.description}</div>
        </div>
        <div className="vp-exp">
          <span>+{totalExp}</span>
          <em>EXP</em>
        </div>
      </div>
      {hint ? (
        <div className="vp-next">
          <span aria-hidden="true">↗</span>
          <span>
            {hint.need} → +{hint.exp_delta} EXP
          </span>
        </div>
      ) : (
        <div className="vp-next vp-next-max">
          <span aria-hidden="true">★</span>
          <span>최고 등급이에요. 여권 비자가 발급됩니다.</span>
        </div>
      )}
    </div>
  );
}

function RequirementChecklist({ items }) {
  return (
    <ul className="req-checks">
      {items.map((it) => (
        <li key={it.key} className={it.met ? 'met' : ''}>
          <span aria-hidden="true">{it.met ? '●' : '○'}</span>
          <span>{it.label}</span>
        </li>
      ))}
    </ul>
  );
}

export default function StampForm({ navigate }) {
  const { addStamp, previewStamp, recentAreas } = useApp();
  const photoInputRef = useRef(null);

  const [placeName, setPlaceName] = useState('');
  const [area, setArea] = useState('성수');
  const [areaSource, setAreaSource] = useState('suggested');
  const [coords, setCoords] = useState({ latitude: null, longitude: null });
  const [category, setCategory] = useState('cafe');
  const [tags, setTags] = useState([]);
  const [menu, setMenu] = useState('');
  const [purpose, setPurpose] = useState('');
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
      // Optional input — silently skip.
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
        const label = `현재 위치 근처 · ${latitude.toFixed(3)}, ${longitude.toFixed(3)}`;
        setLocationLabel(label);
        setCoords({ latitude, longitude });
        setLocationBusy(false);
      },
      (err) => {
        setLocationBusy(false);
        setLocationError(
          err.code === 1
            ? '위치 권한이 거부됐어요. 브라우저 설정을 확인하세요.'
            : '위치를 확인하지 못했어요.',
        );
      },
      { timeout: 7000, maximumAge: 60_000 },
    );
  };

  const useGeoForArea = () => {
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
        // No reverse-geocoding API yet — surface a coordinate-stamped
        // placeholder the user can rename.
        setArea(`현재 위치 (${latitude.toFixed(2)}, ${longitude.toFixed(2)})`);
        setAreaSource('geolocation');
        setCoords({ latitude, longitude });
        setLocationLabel(`현재 위치 근처 · ${latitude.toFixed(3)}, ${longitude.toFixed(3)}`);
        setLocationBusy(false);
      },
      (err) => {
        setLocationBusy(false);
        setLocationError(
          err.code === 1
            ? '위치 권한이 거부됐어요. 브라우저 설정을 확인하세요.'
            : '위치를 확인하지 못했어요.',
        );
      },
      { timeout: 7000, maximumAge: 60_000 },
    );
  };

  const noteChars = experienceNote.trim().length;
  const noteOk = noteChars >= EXP_NOTE_MIN_CHARS;
  const placeOk = placeName.trim().length > 0;
  const areaOk = area && area.trim().length > 0;
  const categoryOk = !!category;
  const menuOrPurposeOk = !!menu.trim() || !!purpose;
  const tagOk = tags.length >= 1;

  const requirements = [
    { key: 'place',    label: '가게 이름',                 met: placeOk },
    { key: 'area',     label: '지역',                       met: areaOk },
    { key: 'category', label: '카테고리',                   met: categoryOk },
    { key: 'menu',     label: '대표 메뉴 또는 방문 목적',    met: menuOrPurposeOk },
    { key: 'note',     label: `한줄 방문 메모 (${EXP_NOTE_MIN_CHARS}자+)`, met: noteOk },
    { key: 'tags',     label: '태그 1개 이상',              met: tagOk },
  ];

  const allMet = requirements.every((r) => r.met);
  const missing = requirements.filter((r) => !r.met).map((r) => r.label);

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

  const verificationHint = nextVerificationHint({
    photo_data_url: photoDataUrl,
    location_label: locationLabel,
  });

  const onSubmit = (event) => {
    event.preventDefault();
    if (!allMet) {
      setError(
        `도장을 찍기 전에 채워주세요: ${missing.join(', ')}`,
      );
      return;
    }
    setError('');
    const result = addStamp({
      place_name: placeName,
      area,
      area_source: areaSource,
      category,
      tags,
      representative_menu: menu,
      visit_purpose: purpose,
      visited_at: visitedAt,
      experience_note: experienceNote,
      photo_data_url: photoDataUrl,
      location_label: locationLabel,
      latitude: coords.latitude,
      longitude: coords.longitude,
      visit_mood: visitMood,
    });
    if (!result.ok) {
      setError(result.error?.message || '도장을 찍을 수 없어요.');
      return;
    }
    // Stash the just-earned badges so /result/<id> can render them
    // even after a hard refresh — survives nav, doesn't survive tab
    // close (which is fine).
    try {
      window.sessionStorage.setItem(
        `stampport:newBadges:${result.stamp.id}`,
        JSON.stringify(result.newBadges || []),
      );
    } catch {
      // ignore quota — the result screen falls back to the badges grid.
    }
    navigate(`/result/${result.stamp.id}`);
  };

  const recentChips = recentAreas.filter((a) => !SUGGESTED_AREAS.includes(a));

  return (
    <section className="form-stack" style={{ gap: 18 }}>
      <button type="button" className="back-btn" onClick={() => navigate('/passport')}>
        ← 여권으로
      </button>
      <div className="page-head">
        <span className="page-eyebrow">New Stamp</span>
        <h1>오늘의 도장 찍기</h1>
        <p>
          오늘 다녀온 곳을 여권에 남기는 의식이에요. 가게 이름만으로는 도장이
          찍히지 않아요.
        </p>
      </div>

      <VerificationPreview
        level={preview.verification_level}
        hint={verificationHint}
        totalExp={preview.breakdown.total}
      />

      <form className="form-stack" onSubmit={onSubmit}>
        <div className="form-field">
          <label htmlFor="place_name">
            가게 이름 <span className="form-required">필수</span>
          </label>
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
          <label>
            지역 <span className="form-required">필수</span>
          </label>
          <div className="area-picker">
            <input
              type="text"
              className="area-input"
              value={area}
              onChange={(e) => {
                setArea(e.target.value);
                setAreaSource('manual');
              }}
              placeholder="동네 이름을 직접 적어도 돼요"
              maxLength={32}
            />
            <button
              type="button"
              className="btn btn-secondary btn-mini area-geo"
              onClick={useGeoForArea}
              disabled={locationBusy}
            >
              {locationBusy ? '확인 중…' : '📍 현재 위치'}
            </button>
          </div>
          {areaSource === 'geolocation' ? (
            <span className="form-helper is-ok">
              현재 위치 기반이에요. 동네 이름을 직접 수정해도 OK.
            </span>
          ) : null}
          {locationError ? (
            <span className="form-helper" style={{ color: 'var(--color-burgundy)' }}>
              {locationError}
            </span>
          ) : null}
          <div className="area-section">
            <span className="area-section-label">추천 지역</span>
            <div className="tag-grid">
              {SUGGESTED_AREAS.map((a) => (
                <button
                  key={a}
                  type="button"
                  className={`tag-chip ${area === a ? 'active' : ''}`}
                  onClick={() => {
                    setArea(a);
                    setAreaSource('suggested');
                  }}
                >
                  {a}
                </button>
              ))}
            </div>
          </div>
          {recentChips.length ? (
            <div className="area-section">
              <span className="area-section-label">최근 사용한 지역</span>
              <div className="tag-grid">
                {recentChips.map((a) => (
                  <button
                    key={a}
                    type="button"
                    className={`tag-chip ${area === a ? 'active' : ''}`}
                    onClick={() => {
                      setArea(a);
                      setAreaSource('recent');
                    }}
                  >
                    🕘 {a}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </div>

        <div className="form-field">
          <label>
            카테고리 <span className="form-required">필수</span>
          </label>
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
          <label htmlFor="menu">
            대표 메뉴 또는 방문 목적 <span className="form-required">필수</span>
          </label>
          <input
            id="menu"
            type="text"
            value={menu}
            onChange={(e) => setMenu(e.target.value)}
            placeholder="예: 소금빵 / 라떼 / 크림 파스타"
            maxLength={40}
          />
          <div className="purpose-grid">
            {VISIT_PURPOSES.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`mood-chip ${purpose === p.id ? 'active' : ''}`}
                onClick={() =>
                  setPurpose((prev) => (prev === p.id ? '' : p.id))
                }
              >
                <span className="mood-emoji" aria-hidden="true">{p.emoji}</span>
                <span className="mood-label">{p.label}</span>
              </button>
            ))}
          </div>
          <span
            className={`form-helper ${menuOrPurposeOk ? 'is-ok' : ''}`}
            style={{ color: menuOrPurposeOk ? 'var(--color-deep-green)' : undefined }}
          >
            {menuOrPurposeOk
              ? `좋아요 — ${menu.trim() ? `대표 메뉴 "${menu.trim()}"` : `방문 목적 "${visitPurposeLabel(purpose)}"`} 기억해 둘게요.`
              : '대표 메뉴를 적거나, 방문 목적 칩을 하나 골라야 해요.'}
          </span>
        </div>

        <div className="form-field">
          <label htmlFor="experience_note">
            한줄 방문 메모 <span className="form-required">필수</span>
          </label>
          <textarea
            id="experience_note"
            value={experienceNote}
            onChange={(e) => setExperienceNote(e.target.value)}
            placeholder="이 가게에서 가장 기억에 남는 한 가지 — 어떤 기분으로 나왔나요?"
            rows={3}
            maxLength={240}
          />
          <span
            className={`form-helper ${noteOk ? 'is-ok' : ''}`}
            style={{ color: noteOk ? 'var(--color-deep-green)' : 'var(--color-burgundy)' }}
          >
            {noteOk
              ? `좋아요 — ${noteChars}자 작성됨`
              : `${EXP_NOTE_MIN_CHARS}자 이상 적어야 도장이 인증돼요. (현재 ${noteChars}자)`}
          </span>
        </div>

        <div className="form-field">
          <label>오늘의 기분 (선택)</label>
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
          <label>
            방문 인증 <span className="form-helper" style={{ marginLeft: 6 }}>도장 등급이 올라가요</span>
          </label>
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
                    사진까지 추가하면 Photo Stamp(A등급) 또는 Verified Stamp(S등급).
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
                      onClick={() => {
                        setLocationLabel('');
                        setCoords({ latitude: null, longitude: null });
                      }}
                    >
                      해제
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <p className="proof-sub">
                    위치를 추가하면 Location Stamp(B등급)가 돼요.
                  </p>
                  <button
                    type="button"
                    className="btn btn-secondary btn-mini"
                    onClick={handleLocation}
                    disabled={locationBusy}
                  >
                    {locationBusy ? '확인 중…' : '현재 위치 확인'}
                  </button>
                </>
              )}
            </div>
          </div>
        </div>

        <div className="form-field">
          <label>
            태그 (최소 1개 · 최대 5개){' '}
            <span className="form-required">필수</span>
          </label>
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
          <span
            className={`form-helper ${tagOk ? 'is-ok' : ''}`}
            style={{ color: tagOk ? 'var(--color-deep-green)' : undefined }}
          >
            {tagOk
              ? `${tags.length}개 선택됨`
              : '취향 태그 한 개 이상 — 어떤 분위기였나요?'}
          </span>
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

        <div className="form-field">
          <label>도장 찍기 전 체크리스트</label>
          <RequirementChecklist items={requirements} />
        </div>

        {error ? (
          <p className="form-helper" style={{ color: 'var(--color-burgundy)' }}>
            {error}
          </p>
        ) : null}

        <button
          type="submit"
          className="btn btn-primary btn-block"
          disabled={!allMet}
          aria-disabled={!allMet}
          title={allMet ? undefined : `채워주세요: ${missing.join(', ')}`}
        >
          {allMet
            ? `도장 찍기 · ${preview.grade.grade}등급 · +${preview.breakdown.total} EXP`
            : `${missing.length}개 항목이 더 필요해요`}
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
