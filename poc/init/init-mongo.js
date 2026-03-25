// init-mongo.js
// AM Platform POC MongoDB 초기화 스크립트

// am_db 데이터베이스 사용
db = db.getSiblingDB('am_db');

// 현재 년월을 기준으로 동적 컬렉션 생성 (예: send_histories_202603)
var currentDate = new Date();
var year = currentDate.getFullYear();
var month = ('0' + (currentDate.getMonth() + 1)).slice(-2);
var collectionName = 'send_histories_' + year + month;

// 컬렉션 생성
db.createCollection(collectionName);

// 수신번호 기준 인덱스 
db[collectionName].createIndex({ "receiver": 1, "requestedAt": -1 });

// 트랜잭션 ID 기준 유니크 인덱스
db[collectionName].createIndex({ "txId": 1 }, { unique: true });

// 상태별 인덱스
db[collectionName].createIndex({ "status": 1, "channel": 1 });

// 샘플 데이터 1건 적재
db[collectionName].insert({
    "txId": "sample-uuid-0001",
    "requestId": "ext-req-001",
    "channel": "SMS",
    "priority": 1,
    "sender": "01012345678",
    "receiver": "01087654321",
    "status": "SENT",
    "resultCode": "10000",
    "body": "테스트 발송입니다.",
    "requestedAt": new Date(),
    "createdAt": new Date()
});

print("MongoDB initialization complete for collection: " + collectionName);
