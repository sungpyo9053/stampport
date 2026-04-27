import { useState } from 'react';
import { useApp } from '../context/appContext.js';

export default function Login({ navigate }) {
  const { login } = useApp();
  const [nickname, setNickname] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');

  const onSubmit = (event) => {
    event.preventDefault();
    if (!nickname.trim()) {
      setError('닉네임을 입력해 주세요.');
      return;
    }
    if (!email.trim() || !email.includes('@')) {
      setError('이메일 형식을 확인해 주세요.');
      return;
    }
    login({ nickname: nickname.trim(), email: email.trim() });
    navigate('/passport');
  };

  return (
    <section className="login">
      <div className="login-hero">
        <div className="stamp-mark" aria-hidden="true">
          <span style={{ fontSize: 11, letterSpacing: '0.2em' }}>WELCOME</span>
        </div>
        <h1>나의 로컬 여권 시작하기</h1>
        <p>
          MVP에서는 닉네임과 이메일만으로 로그인합니다.
          <br />
          데이터는 이 기기 안에만 저장돼요.
        </p>
      </div>

      <form className="form-stack" onSubmit={onSubmit}>
        <div className="form-field">
          <label htmlFor="nickname">닉네임</label>
          <input
            id="nickname"
            type="text"
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
            placeholder="예: 빵지순례러"
            maxLength={20}
            autoComplete="nickname"
          />
        </div>
        <div className="form-field">
          <label htmlFor="email">이메일</label>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            autoComplete="email"
          />
          <span className="form-helper">
            이메일은 사용자 식별용으로만 쓰이며 외부에 공개되지 않습니다.
          </span>
        </div>
        {error ? (
          <p className="form-helper" style={{ color: 'var(--color-burgundy)' }}>
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn-primary btn-block">
          여권 만들고 시작하기
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-block"
          onClick={() => navigate('/')}
        >
          돌아가기
        </button>
      </form>
    </section>
  );
}
