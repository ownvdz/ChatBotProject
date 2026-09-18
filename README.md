# 🤖 Discord Assistant ChatBot

[![Web Presentation](https://img.shields.io/badge/Presentation-GitHub_Pages-indigo?style=for-the-badge&logo=github)](https://ownvdz.github.io/ChatBotProject/)

> 🔗 **프로젝트 발표 웹사이트:** [https://ownvdz.github.io/ChatBotProject/](https://ownvdz.github.io/ChatBotProject/)

---

## 📌 프로젝트 소개
개인 맞춤형 일상 및 학교 생활 편의를 위해 개발된 **디스코드 챗봇**입니다. 
국토교통부(TAGO) 공공데이터 Open API를 연동하여 실시간 대중교통 정보를 제공하며, Oracle Cloud 인프라를 통해 24시간 365일 무중단으로 운영됩니다.

---

## ✨ 핵심 기능

* **🚌 실시간 버스 도착 정보 (`/버스`)**
  * 국토교통부(TAGO) 도착정보 조회 API 연동
  * 주요 정류장(주안역환승정류장, 정석항공과학고 등) 511번 버스 양방향 실시간 도착 예정 시간 안내
  * 도착 정보와 GPS 위치 API 데이터를 교차 검증하여 운행 중인 차량 번호 추정 제공

* **🚇 지하철 운행/시간표 안내 (`/지하철`)**
  * 인천2호선(마전역, 주안역 등) 맞춤 실시간/시간표 데이터 파싱
  * 현재 시간 기준 출발/도착까지 남은 시간 자동 계산

* **☁️ 24/7 클라우드 무중단 가동**
  * Oracle Cloud Infrastructure (Ubuntu 22.04 LTS) 기반 상시 운용
  * `systemd` 데몬 등록을 통한 비정상 종료 시 자동 재시작 환경 구축

---

## 🛠 기술 스택 (Tech Stack)

| 구분 | 기술 / 서비스 |
| :--- | :--- |
| **Language** | Python 3.10+ |
| **Library** | discord.py, requests |
| **Infrastructure** | Oracle Cloud Infrastructure (OCI) |
| **OS** | Ubuntu 22.04 LTS |
| **API** | 국토교통부(TAGO) 버스/지하철 정보 API |
| **Presentation** | HTML5, Tailwind CSS, GitHub Pages |

---

## 🚀 향후 개발 로드맵 (Roadmap)

- [ ] **수업 시간표 CRUD**
  - 고정 JSON 방식에서 DB 연동으로 전환
  - 슬래시 명령어를 통한 과목 추가/수정/삭제 기능 구현
- [ ] **대중교통 즐겨찾기 커스텀**
  - 사용자별 자주 이용하는 버스 정류장 및 지하철역 개인화 저장
- [ ] **학점 계산기 기능**
  - 과목별 성적 입력 및 평점, 졸업 이수 학점 계산 서비스 추가 예정