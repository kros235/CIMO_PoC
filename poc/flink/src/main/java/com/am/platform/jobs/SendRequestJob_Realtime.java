package com.am.platform.jobs;

import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * 실시간성 발송 요청(sendMethodCode 03,04,05) 처리 Flink Job.
 *
 * ⭐️ 신규(Day 8, 실시간·배치 요청 라인 분리 - 방안 A): 기존 SendRequestJob.java가
 * 담당하던 파이프라인 중 실시간 요청 부분만 독립된 Job으로 분리했다. 배치 Job
 * (SendRequestJob_Batch)과 완전히 별도의 Kafka Consumer Group·Flink Job이므로,
 * 배치가 몰려도 이 Job의 처리 자원에는 물리적으로 영향이 없다.
 *
 * 파이프라인 조립 로직은 RequestPipelineBuilder(공통 클래스)를 그대로 사용하며,
 * 이 클래스는 "어떤 토픽을 구독할지"만 지정하는 진입점 역할만 한다.
 *
 * 환경변수:
 *   KAFKA_BOOTSTRAP_SERVERS         (기본: kafka:9092)
 *   KAFKA_GROUP_ID_REQUEST_REALTIME (기본: am-flink-request-realtime-group)
 */
public class SendRequestJob_Realtime {

    private static final Logger LOG = LoggerFactory.getLogger(SendRequestJob_Realtime.class);

    private static final String BOOTSTRAP_SERVERS =
            System.getenv().getOrDefault("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092");
    private static final String GROUP_ID =
            System.getenv().getOrDefault("KAFKA_GROUP_ID_REQUEST_REALTIME", "am-flink-request-realtime-group");
    private static final String TOPIC_REQUEST_REALTIME = "topic.send.request.realtime";

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

        RequestPipelineBuilder.build(env, BOOTSTRAP_SERVERS, GROUP_ID, TOPIC_REQUEST_REALTIME, "Realtime");

        LOG.info("[SendRequestJob_Realtime] Job 시작: bootstrapServers={}, groupId={}, topic={}",
                BOOTSTRAP_SERVERS, GROUP_ID, TOPIC_REQUEST_REALTIME);

        env.execute("AM-SendRequestJob-Realtime");
    }
}
