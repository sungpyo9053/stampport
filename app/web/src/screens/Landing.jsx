export default function Landing({ navigate }) {
  return (
    <section className="landing">
      <div className="passport-cover">
        <span className="cover-tag">Stampport · 스탬포트</span>
        <h1>
          오늘 다녀온 곳,
          <br />
          스탬포트에 도장 찍기.
        </h1>
        <p className="cover-sub">
          먹고 머문 곳들이,
          <br />
          나만의 로컬 여권이 됩니다.
        </p>
        <div className="cover-stamp" aria-hidden="true">
          <span>SINCE</span>
          <strong>YOU</strong>
          <span>VISIT</span>
        </div>
      </div>

      <div className="landing-features">
        <div className="landing-feature">
          <span className="emoji" aria-hidden="true">📓</span>
          <h3>나만의 여권</h3>
          <p>방문한 카페·빵집·맛집을 도장으로 모아 보세요.</p>
        </div>
        <div className="landing-feature">
          <span className="emoji" aria-hidden="true">🏅</span>
          <h3>뱃지와 칭호</h3>
          <p>지역·태그 조건을 채워 칭호를 획득해요.</p>
        </div>
        <div className="landing-feature">
          <span className="emoji" aria-hidden="true">🎯</span>
          <h3>이번 주 퀘스트</h3>
          <p>다음 방문이 더 즐거워지는 작은 미션.</p>
        </div>
        <div className="landing-feature">
          <span className="emoji" aria-hidden="true">✨</span>
          <h3>감성 공유 카드</h3>
          <p>SNS에 자랑하기 좋은 스탬프 카드 자동 생성.</p>
        </div>
      </div>

      <div className="landing-cta">
        <button
          type="button"
          className="btn btn-primary btn-block"
          onClick={() => navigate('/login')}
        >
          스탬프 찍으러 가기
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-block"
          onClick={() => navigate('/login')}
        >
          내 여권 만들기
        </button>
        <p className="landing-tagline">
          로그인은 닉네임과 이메일만 있으면 충분해요.
        </p>
      </div>
    </section>
  );
}
