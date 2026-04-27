import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  checkStampLimits,
  clearUser,
  loadProfileMeta,
  loadRecentAreas,
  loadStamps,
  loadUser,
  makeNewProfile,
  pushRecentArea,
  saveProfileMeta,
  saveStamps,
  saveUser,
} from '../utils/storage.js';
import { BADGE_DEFS, computeBadges, newlyEarnedBadges } from '../data/badges.js';
import { computeQuests } from '../data/quests.js';
import {
  expGainBreakdown,
  levelFor,
  levelProgress,
  stampGradeFor,
  totalExp,
  verificationLevelFor,
} from '../utils/leveling.js';
import { generateKickPoints } from '../utils/kickPoints.js';
import { AppContext } from './appContext.js';

function makeStampId() {
  return `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export function AppProvider({ children }) {
  const [user, setUser] = useState(() => loadUser());
  const [stamps, setStamps] = useState(() => {
    const u = loadUser();
    return u ? loadStamps(u.user_id) : [];
  });
  const [profileMeta, setProfileMeta] = useState(() => {
    const u = loadUser();
    return u ? loadProfileMeta(u.user_id) : { selected_title_id: null };
  });
  const [recentAreas, setRecentAreas] = useState(() => {
    const u = loadUser();
    return u ? loadRecentAreas(u.user_id) : [];
  });

  useEffect(() => {
    if (user) saveUser(user);
  }, [user]);

  useEffect(() => {
    if (user) saveStamps(user.user_id, stamps);
  }, [user, stamps]);

  useEffect(() => {
    if (user) saveProfileMeta(user.user_id, profileMeta);
  }, [user, profileMeta]);

  const loginAs = useCallback(
    ({
      provider = 'guest',
      nickname,
      email = null,
      provider_user_id = null,
      avatar_style,
      passport_title,
    } = {}) => {
      const existing = loadUser();
      let nextUser;
      const isSameUser = !!existing
        && existing.provider === provider
        && (provider_user_id
          ? existing.provider_user_id === provider_user_id
          : email
          ? existing.email === email
          : provider === 'guest');
      if (isSameUser) {
        nextUser = {
          ...existing,
          nickname: nickname || existing.nickname,
          provider_user_id: provider_user_id ?? existing.provider_user_id,
          email: email ?? existing.email,
          avatar_style: avatar_style || existing.avatar_style,
          passport_title: passport_title || existing.passport_title,
          last_login_at: new Date().toISOString(),
        };
      } else {
        nextUser = makeNewProfile({
          provider,
          nickname,
          email,
          provider_user_id,
          avatar_style,
          passport_title,
        });
      }
      setUser(nextUser);
      setStamps(loadStamps(nextUser.user_id));
      setProfileMeta(loadProfileMeta(nextUser.user_id));
      setRecentAreas(loadRecentAreas(nextUser.user_id));
      saveUser(nextUser);
      return nextUser;
    },
    [],
  );

  const login = useCallback(
    ({ nickname, email }) => loginAs({ provider: 'guest', nickname, email }),
    [loginAs],
  );

  const logout = useCallback(() => {
    clearUser();
    setUser(null);
    setStamps([]);
    setProfileMeta({ selected_title_id: null });
    setRecentAreas([]);
  }, []);

  // addStamp now returns { ok, stamp?, error?, newBadges? }. The form
  // surfaces error.message; the result screen reads newBadges from
  // sessionStorage so it can render a "방금 받은 뱃지" row even after
  // a refresh.
  const addStamp = useCallback(
    (input) => {
      const previous = stamps;
      const visited_at = input.visited_at || new Date().toISOString().slice(0, 10);
      const candidate = {
        place_name: input.place_name?.trim() || '',
        area: input.area || '기타',
        area_source: input.area_source || 'manual',
        category: input.category || 'cafe',
        tags: input.tags || [],
        representative_menu: input.representative_menu?.trim() || '',
        visit_purpose: input.visit_purpose || '',
        visited_at,
        experience_note: input.experience_note?.trim() || '',
        photo_data_url: input.photo_data_url || '',
        location_label: input.location_label?.trim() || '',
        latitude: typeof input.latitude === 'number' ? input.latitude : null,
        longitude: typeof input.longitude === 'number' ? input.longitude : null,
        visit_mood: input.visit_mood || '',
      };

      const limit = checkStampLimits(previous, candidate, visited_at);
      if (!limit.ok) {
        return { ok: false, error: limit };
      }

      const stamp = {
        id: makeStampId(),
        user_id: user?.user_id,
        ...candidate,
        verification_status: 'unverified',
        trust_score: 0,
        created_at: new Date().toISOString(),
      };
      stamp.verification_level = verificationLevelFor(stamp);
      stamp.kick_points = generateKickPoints(stamp);
      const breakdown = expGainBreakdown(stamp, previous);
      stamp.exp_breakdown = breakdown.items;
      stamp.exp_gained = breakdown.total;
      stamp.grade = stampGradeFor(stamp);

      const beforeBadges = computeBadges(previous);
      const nextStamps = [stamp, ...previous];
      const afterBadges = computeBadges(nextStamps);
      const newBadges = newlyEarnedBadges(beforeBadges, afterBadges);

      setStamps(nextStamps);
      if (user?.user_id) {
        pushRecentArea(user.user_id, stamp.area);
        setRecentAreas(loadRecentAreas(user.user_id));
      }
      return { ok: true, stamp, newBadges };
    },
    [stamps, user],
  );

  const setSelectedTitle = useCallback((badgeId) => {
    setProfileMeta((prev) => ({ ...prev, selected_title_id: badgeId }));
  }, []);

  const exp = useMemo(() => totalExp(stamps), [stamps]);
  const level = useMemo(() => levelFor(exp), [exp]);
  const levelInfo = useMemo(() => levelProgress(exp), [exp]);
  const badges = useMemo(() => computeBadges(stamps), [stamps]);
  const quests = useMemo(() => computeQuests(stamps), [stamps]);

  const earnedBadges = useMemo(() => badges.filter((b) => b.earned), [badges]);

  const selectedTitle = useMemo(() => {
    const id = profileMeta.selected_title_id;
    if (id) {
      const def = BADGE_DEFS.find((d) => d.id === id);
      const earned = earnedBadges.find((b) => b.id === id);
      if (def && earned) return def.titleLabel;
    }
    if (earnedBadges.length > 0) {
      const def = BADGE_DEFS.find((d) => d.id === earnedBadges[earnedBadges.length - 1].id);
      return def?.titleLabel || '로컬 미식 새내기';
    }
    return '로컬 미식 새내기';
  }, [profileMeta, earnedBadges]);

  const stampById = useCallback(
    (id) => stamps.find((s) => s.id === id) || null,
    [stamps],
  );

  // Live preview for StampForm — what would `addStamp(input)` award if
  // committed right now? Pure; no side effects.
  const previewStamp = useCallback(
    (input) => {
      const candidate = {
        place_name: input.place_name || '',
        area: input.area || '기타',
        category: input.category || 'cafe',
        tags: input.tags || [],
        experience_note: input.experience_note || '',
        photo_data_url: input.photo_data_url || '',
        location_label: input.location_label || '',
        visit_mood: input.visit_mood || '',
      };
      return {
        breakdown: expGainBreakdown(candidate, stamps),
        grade: stampGradeFor(candidate),
        verification_level: verificationLevelFor(candidate),
      };
    },
    [stamps],
  );

  const streakLast7Days = useMemo(() => {
    const days = new Set();
    const now = Date.now();
    const SEVEN_DAYS = 7 * 24 * 60 * 60 * 1000;
    for (const s of stamps) {
      const t = s.visited_at ? Date.parse(s.visited_at) : NaN;
      if (!Number.isFinite(t)) continue;
      if (now - t > SEVEN_DAYS) continue;
      days.add(s.visited_at.slice(0, 10));
    }
    return days.size;
  }, [stamps]);

  // The "다음 목표" hint surfaced on MyPassport — pick the in-progress
  // badge with the highest progress ratio so the user sees something
  // they're close to finishing. Falls back to the first locked badge
  // for new players.
  const nextGoal = useMemo(() => {
    const inProgress = badges
      .filter((b) => !b.earned && b.progress > 0)
      .map((b) => ({ ...b, ratio: b.progress / b.required }))
      .sort((a, b) => b.ratio - a.ratio);
    if (inProgress.length) return inProgress[0];
    return badges.find((b) => !b.earned) || null;
  }, [badges]);

  const value = useMemo(
    () => ({
      user,
      stamps,
      addStamp,
      login,
      loginAs,
      logout,
      exp,
      level,
      levelInfo,
      badges,
      earnedBadges,
      quests,
      selectedTitle,
      profileMeta,
      setSelectedTitle,
      stampById,
      previewStamp,
      streakLast7Days,
      recentAreas,
      nextGoal,
    }),
    [
      user,
      stamps,
      addStamp,
      login,
      loginAs,
      logout,
      exp,
      level,
      levelInfo,
      badges,
      earnedBadges,
      quests,
      selectedTitle,
      profileMeta,
      setSelectedTitle,
      stampById,
      previewStamp,
      streakLast7Days,
      recentAreas,
      nextGoal,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
