const SEAL_PATH_CAFE = `M10,28 L30,28 L28,16 L12,16 Z
M11,16 L29,16
M28,21 C34,21 34,26 28,26
M15,13 C15,10 13,9 15,7
M20,13 C20,10 18,9 20,7
M25,13 C25,10 23,9 25,7`;

const SEAL_PATH_BAKERY = `M6,28 C6,22 8,14 20,12 C32,14 34,22 34,28 Z
M6,28 L34,28
M14,28 L18,18
M20,28 L20,16
M26,28 L22,18`;

const SEAL_PATH_RESTAURANT = `M10,8 L10,14
M13,8 L13,17 L13,28
M16,8 L16,14
M10,14 C10,17 16,17 16,14
M27,8 C29,8 31,12 27,16 L27,28`;

const SEAL_PATH_DESSERT = `M8,30 L20,8 L32,30 Z
M10,24 L30,24
M9,27 L31,27
M17,11 C17,8 23,8 23,11`;

const CATEGORY_PATHS = {
  cafe: SEAL_PATH_CAFE,
  bakery: SEAL_PATH_BAKERY,
  restaurant: SEAL_PATH_RESTAURANT,
  dessert: SEAL_PATH_DESSERT,
};

function tierForGrade(gradeLetter) {
  if (gradeLetter === 'S' || gradeLetter === 'A') return 3;
  if (gradeLetter === 'B') return 2;
  return 1;
}

function FrameTier1({ stroke, fill }) {
  return (
    <g>
      <circle cx="40" cy="40" r="30" stroke={stroke} fill={fill} strokeWidth="2" />
      <circle
        cx="40"
        cy="40"
        r="26"
        stroke={stroke}
        fill="none"
        strokeWidth="0.8"
        strokeDasharray="3 3"
      />
    </g>
  );
}

function FrameTier2({ stroke, fill }) {
  return (
    <g>
      <path
        d="M10,8 L70,8 L70,48 C70,62 56,72 40,76 C24,72 10,62 10,48 Z"
        stroke={stroke}
        fill={fill}
        strokeWidth="2"
      />
      <path
        d="M14,12 L66,12 L66,46 C66,59 53,69 40,73 C27,69 14,59 14,46 Z"
        stroke={stroke}
        fill="none"
        strokeWidth="0.8"
      />
    </g>
  );
}

function FrameTier3({ stroke, fill, withDots }) {
  return (
    <g>
      <path
        d="M12,58 L18,24 L32,42 L40,16 L48,42 L62,24 L68,58 Z"
        stroke={stroke}
        fill={fill}
        strokeWidth="2"
      />
      <path d="M10,62 L70,62" stroke={stroke} fill="none" strokeWidth="1.5" />
      {withDots ? (
        <g>
          <circle cx="28" cy="20" r="2" fill={stroke} />
          <circle cx="40" cy="14" r="2.5" fill={stroke} />
          <circle cx="52" cy="20" r="2" fill={stroke} />
        </g>
      ) : null}
    </g>
  );
}

export default function StampSeal({ category, grade, size = 80 }) {
  const gradeLetter = grade?.grade || 'C';
  const tier = tierForGrade(gradeLetter);
  const iconPath = CATEGORY_PATHS[category] || SEAL_PATH_CAFE;

  let stroke;
  let fill;
  let Frame;
  if (tier === 1) {
    stroke = 'rgba(201,162,58,0.72)';
    fill = 'rgba(201,162,58,0.06)';
    Frame = <FrameTier1 stroke={stroke} fill={fill} />;
  } else if (tier === 2) {
    stroke = '#6e1f2a';
    fill = 'rgba(110,31,42,0.06)';
    Frame = <FrameTier2 stroke={stroke} fill={fill} />;
  } else {
    stroke = '#c9a23a';
    fill = 'rgba(201,162,58,0.08)';
    Frame = <FrameTier3 stroke={stroke} fill={fill} withDots={gradeLetter === 'S'} />;
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 80 80"
      xmlns="http://www.w3.org/2000/svg"
      style={{ color: stroke }}
      aria-hidden="true"
    >
      {Frame}
      <g transform="translate(20,20) scale(1.0)" stroke="currentColor" fill="none" strokeWidth="1.5">
        <path d={iconPath} strokeLinecap="round" strokeLinejoin="round" />
      </g>
    </svg>
  );
}
