package com.am.platform.util;

/**
 * 발송 결과코드 분류기.
 *
 * 결과코드 → Disposition 매핑:
 *   10000        → STORE   (성공, 이력 DB에 저장)
 *   40001~40008  → DLQ     (영구 실패, 재처리 불가)
 *   50001~50004  → RETRY   (재처리 가능)
 *   50002        → FALLBACK (RCS→SMS 채널 변경 재발송)
 */
public class ResultCodeClassifier {

    public static final String DISPOSITION_STORE    = "STORE";
    public static final String DISPOSITION_RETRY    = "RETRY";
    public static final String DISPOSITION_FALLBACK = "FALLBACK";
    public static final String DISPOSITION_DLQ      = "DLQ";

    public static final String CODE_SUCCESS         = "10000";
    public static final String CODE_RCS_FALLBACK    = "50002";

    private ResultCodeClassifier() {}

    /**
     * 결과코드를 받아 Disposition 문자열을 반환한다.
     *
     * @param resultCode 발송 결과코드
     * @return STORE / RETRY / FALLBACK / DLQ
     */
    public static String classify(String resultCode) {
        if (resultCode == null) {
            return DISPOSITION_DLQ;
        }
        if (CODE_SUCCESS.equals(resultCode)) {
            return DISPOSITION_STORE;
        }
        if (CODE_RCS_FALLBACK.equals(resultCode)) {
            return DISPOSITION_FALLBACK;
        }
        if (resultCode.startsWith("4")) {
            return DISPOSITION_DLQ;
        }
        if (resultCode.startsWith("5")) {
            return DISPOSITION_RETRY;
        }
        // 알 수 없는 코드는 DLQ로 처리 (안전 우선)
        return DISPOSITION_DLQ;
    }

    /** 성공 여부 */
    public static boolean isSuccess(String resultCode) {
        return CODE_SUCCESS.equals(resultCode);
    }

    /** 영구 실패 여부 (재처리 불가) */
    public static boolean isPermanentFailure(String resultCode) {
        return resultCode != null && resultCode.startsWith("4");
    }

    /** 재처리 가능 실패 여부 */
    public static boolean isRetryable(String resultCode) {
        return resultCode != null && resultCode.startsWith("5");
    }

    /** RCS fallback 여부 */
    public static boolean isRcsFallback(String resultCode) {
        return CODE_RCS_FALLBACK.equals(resultCode);
    }
}