import http from 'k6/http';
import { check, sleep } from 'k6';

export let options = {
  vus: 50,
  duration: '1m',
};

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:5050';

export default function () {
  const payload = JSON.stringify({
    form_id: 1,
    form_title: 'Load test',
    student_name: 'Load Tester',
    answers: [
      { question: 'How was the course?', type: 'rating_5', answer: '4' },
      { question: 'Comments', type: 'text', answer: 'Good course' }
    ]
  });

  const params = {
    headers: {
      'Content-Type': 'application/json'
    }
  };

  const res = http.post(`${BASE}/api/submit_feedback`, payload, params);
  check(res, { 'status is 200': (r) => r.status === 200 });
  sleep(1);
}
