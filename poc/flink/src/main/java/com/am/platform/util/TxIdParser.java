package com.am.platform.util;

/**
 * 35자리 txId 파싱 및 검증 유틸리티.
 *
 * txId 구조:
 *   [0  ~12] messageId       = 13자리 숫자 (유니크)
 *   [13 ~14] sendMethodCode  = 2자리 숫자 (01~05)
 *   [15 ~17] dayOfYear       = 3자리 숫자 (001~366)
 *   [18 ~20] senderCode      = 3자리 숫자 (유니크)
 *   [21 ~34] sequence        = 14자리 숫자
 *
 * 유효 sendMethodCode:
 *   01~02 = 배치성 발송
 *   03    = 온라인(실시간) 발송
 *   04~05 = 준실시간 발송
 */
public class TxIdParser {

    public static final int TX_ID_LENGTH = 35;

    private TxIdParser() {}

    /**
     * txId 기본 유효성 검사.
     * - 길이 35자리
     * - 전체 숫자 여부
     * - sendMethodCode 01~05 범위
     * - dayOfYear 001~366 범위
     */
    public static boolean isValid(String txId) {
        if (txId == null || txId.length() != TX_ID_LENGTH) {
            return false;
        }
        if (!txId.matches("\\d{35}")) {
            return false;
        }
        int methodCode = Integer.parseInt(txId.substring(13, 15));
        if (methodCode < 1 || methodCode > 5) {
            return false;
        }
        int dayOfYear = Integer.parseInt(txId.substring(15, 18));
        if (dayOfYear < 1 || dayOfYear > 366) {
            return false;
        }
        return true;
    }

    /** messageId (13자리) 추출 */
    public static String getMessageId(String txId) {
        validateLength(txId);
        return txId.substring(0, 13);
    }

    /** sendMethodCode (2자리) 추출 */
    public static String getSendMethodCode(String txId) {
        validateLength(txId);
        return txId.substring(13, 15);
    }

    /** dayOfYear (3자리) 추출 */
    public static String getDayOfYear(String txId) {
        validateLength(txId);
        return txId.substring(15, 18);
    }

    /** senderCode (3자리) 추출 */
    public static String getSenderCode(String txId) {
        validateLength(txId);
        return txId.substring(18, 21);
    }

    /** sequence (14자리) 추출 */
    public static String getSequence(String txId) {
        validateLength(txId);
        return txId.substring(21, 35);
    }

    /**
     * 발송 방식 분류
     *   REALTIME  = 코드 03
     *   BATCH     = 코드 01, 02
     *   NEAR_RT   = 코드 04, 05
     */
    public static String getSendType(String txId) {
        validateLength(txId);
        String code = txId.substring(13, 15);
        switch (code) {
            case "03": return "REALTIME";
            case "01":
            case "02": return "BATCH";
            case "04":
            case "05": return "NEAR_RT";
            default:   return "UNKNOWN";
        }
    }

    private static void validateLength(String txId) {
        if (txId == null || txId.length() != TX_ID_LENGTH) {
            throw new IllegalArgumentException(
                "txId must be exactly " + TX_ID_LENGTH + " digits, got: "
                + (txId == null ? "null" : txId.length()));
        }
    }
}