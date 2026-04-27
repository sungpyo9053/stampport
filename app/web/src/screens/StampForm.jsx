import { useState } from 'react';
import { useApp } from '../context/appContext.js';
import { AREAS, CATEGORIES, TAGS } from '../data/options.js';

function todayInputValue() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

export default function StampForm({ navigate }) {
  const { addStamp } = useApp();
  const [placeName, setPlaceName] = useState('');
  const [area, setArea] = useState('성수');
  const [category, setCategory] = useState('cafe');
  const [tags, setTags] = useState([]);
  const [menu, setMenu] = useState('');
  const [visitedAt, setVisitedAt] = useState(todayInputValue());
  const [error, setError] = useState('');

  const toggleTag = (tag) => {
    setTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag],
    );
  };

  const onSubmit = (event) => {
    event.preventDefault();
    if (!placeName.trim()) {
      setError('가게 이름을 입력해 주세요.');
      return;
    }
    const stamp = addStamp({
      place_name: placeName,
      area,
      category,
      tags,
      representative_menu: menu,
      visited_at: visitedAt,
    });
    navigate(`/result/${stamp.id}`);
  };

  return (
    <section className="form-stack" style={{ gap: 18 }}>
      <button type="button" className="back-btn" onClick={() => navigate('/passport')}>
        ← 여권으로
      </button>
      <div className="page-head">
        <span className="page-eyebrow">New Stamp</span>
        <h1>오늘의 도장 찍기</h1>
        <p>다녀온 곳을 여권에 남겨 주세요.</p>
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
          <span className="form-helper">
            MVP는 사진/QR/위치 인증 없이 manual 인증으로 저장돼요.
          </span>
        </div>

        {error ? (
          <p className="form-helper" style={{ color: 'var(--color-burgundy)' }}>
            {error}
          </p>
        ) : null}

        <button type="submit" className="btn btn-primary btn-block">
          스탬프 찍기
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
